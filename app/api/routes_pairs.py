import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    get_alert_service,
    get_connection_manager,
    get_market_service,
    get_orderbook_service,
    get_pair_stats_service,
    get_precision_service,
    normalize_symbol,
)
from app.api.routes_market import to_market_view
from app.api.routes_notes import WRITE_FORBIDDEN, WriteGuards
from app.database import get_session
from app.models import Note, Tag
from app.schemas.alert import AlertOut
from app.schemas.error import (
    RATE_LIMITED,
    UNKNOWN_SYMBOL,
    error_response,
    validation_error,
    validation_or_error,
)
from app.schemas.market import WindowStats
from app.schemas.pair import PairSummary
from app.schemas.tag import (
    PairMeta,
    PairNoteUpdate,
    PairTagsUpdate,
    TagCreate,
    TagOut,
    TagUpdate,
    TagWithCount,
)
from app.services import pair_meta_service
from app.services.alert_service import AlertService
from app.services.exchange_client import ExchangeUnavailableError
from app.services.market_data_service import MarketDataService
from app.services.orderbook_service import OrderBookService
from app.services.pair_meta_service import (
    MAX_TAGS,
    MAX_TAGS_PER_PAIR,
    DuplicateTagNameError,
    PairTagLimitError,
    TagLimitError,
    UnknownTagError,
)
from app.services.pair_stats_service import PairStatsService
from app.services.precision_service import PrecisionService
from app.websocket.manager import ConnectionManager

logger = logging.getLogger(__name__)

tags_router = APIRouter(prefix="/api/tags", tags=["tags"])
pairs_router = APIRouter(prefix="/api/pairs", tags=["pairs"])

Session = Annotated[AsyncSession, Depends(get_session)]
MarketService = Annotated[MarketDataService, Depends(get_market_service)]
Manager = Annotated[ConnectionManager, Depends(get_connection_manager)]
OrderBooks = Annotated[OrderBookService, Depends(get_orderbook_service)]
Precision = Annotated[PrecisionService, Depends(get_precision_service)]
PairStats = Annotated[PairStatsService, Depends(get_pair_stats_service)]
Alerts = Annotated[AlertService, Depends(get_alert_service)]

TRIGGERED_ALERTS_LIMIT = 10
STATS_UNAVAILABLE = "Berza trenutno nije dostupna"

TOO_MANY_TAGS = f"Najviše {MAX_TAGS} oznaka; obriši neku pre dodavanja nove"
TOO_MANY_PAIR_TAGS = f"Par može imati najviše {MAX_TAGS_PER_PAIR} oznaka"
DUPLICATE_NAME = "Oznaka sa tim imenom već postoji"
TAG_NOT_FOUND_DETAIL = "Oznaka ne postoji"
TAG_NOT_FOUND = error_response("No tag with this id.", TAG_NOT_FOUND_DETAIL)
NAME_CONFLICT = error_response("Another tag has this name (case is ignored).", DUPLICATE_NAME)


async def broadcast_tags(session: AsyncSession, manager: ConnectionManager) -> None:
    tags = await pair_meta_service.list_tags(session)
    data = [tag.model_dump(mode="json") for tag in tags]
    await manager.broadcast({"type": "tags_updated", "data": data})


async def broadcast_pair_meta(manager: ConnectionManager, meta: PairMeta) -> None:
    await manager.broadcast({"type": "pair_meta_updated", "data": meta.model_dump(mode="json")})


# ---------- Tags ----------


@tags_router.get(
    "",
    response_model=list[TagWithCount],
    summary="List tags",
    description="Every tag, sorted by name, with the number of pairs that have it.",
)
async def list_tags(session: Session) -> list[TagWithCount]:
    return await pair_meta_service.list_tags(session)


@tags_router.post(
    "",
    response_model=TagOut,
    status_code=201,
    dependencies=WriteGuards,
    summary="Create a tag",
    description=(
        f"At most {MAX_TAGS} tags in total. Every browser receives a tags_updated message "
        "with all tags."
    ),
    responses={
        400: error_response(f"Already {MAX_TAGS} tags.", TOO_MANY_TAGS),
        403: WRITE_FORBIDDEN,
        409: NAME_CONFLICT,
        422: validation_error("Empty or too long name, or a color outside the fixed list."),
        429: RATE_LIMITED,
    },
)
async def create_tag(payload: TagCreate, session: Session, manager: Manager) -> Tag:
    try:
        tag = await pair_meta_service.create_tag(session, payload)
    except TagLimitError as exc:
        raise HTTPException(status_code=400, detail=TOO_MANY_TAGS) from exc
    except DuplicateTagNameError as exc:
        raise HTTPException(status_code=409, detail=DUPLICATE_NAME) from exc
    await broadcast_tags(session, manager)
    return tag


@tags_router.patch(
    "/{tag_id}",
    response_model=TagOut,
    dependencies=WriteGuards,
    summary="Rename or recolor a tag",
    description=(
        "Changes the name, the color or both. Every browser receives a tags_updated message."
    ),
    responses={
        403: WRITE_FORBIDDEN,
        404: TAG_NOT_FOUND,
        409: NAME_CONFLICT,
        422: validation_error("Non-integer id, nothing to change, or an invalid name or color."),
        429: RATE_LIMITED,
    },
)
async def update_tag(tag_id: int, payload: TagUpdate, session: Session, manager: Manager) -> Tag:
    try:
        tag = await pair_meta_service.update_tag(session, tag_id, payload)
    except DuplicateTagNameError as exc:
        raise HTTPException(status_code=409, detail=DUPLICATE_NAME) from exc
    if tag is None:
        raise HTTPException(status_code=404, detail=TAG_NOT_FOUND_DETAIL)
    await broadcast_tags(session, manager)
    return tag


@tags_router.delete(
    "/{tag_id}",
    status_code=204,
    dependencies=WriteGuards,
    summary="Delete a tag",
    description=(
        "Deletes the tag and removes it from every pair. Every browser receives tags_updated, "
        "then pair_meta_updated for each pair that had the tag."
    ),
    responses={
        403: WRITE_FORBIDDEN,
        404: TAG_NOT_FOUND,
        422: validation_error("Non-integer id."),
        429: RATE_LIMITED,
    },
)
async def delete_tag(tag_id: int, session: Session, manager: Manager) -> Response:
    symbols = await pair_meta_service.delete_tag(session, tag_id)
    if symbols is None:
        raise HTTPException(status_code=404, detail=TAG_NOT_FOUND_DETAIL)
    await broadcast_tags(session, manager)
    for meta in await pair_meta_service.pair_metas(session, symbols):
        await broadcast_pair_meta(manager, meta)
    return Response(status_code=204)


# ---------- Pairs ----------


@pairs_router.get(
    "/meta",
    response_model=list[PairMeta],
    summary="Tags and notes of every pair",
    description="One entry per supported pair, in the configured order; note is null if none.",
)
async def list_pair_meta(session: Session, service: MarketService) -> list[PairMeta]:
    return await pair_meta_service.pair_metas(session, service.settings.supported_symbols)


@pairs_router.get(
    "/{symbol}/summary",
    response_model=PairSummary,
    summary="Everything about one pair",
    description=(
        "Live market data, 7-day statistics, decimals, order book, tags, note, alerts and the "
        "number of chart notes of one pair. 7-day statistics come from Binance REST, cached "
        "for 60 seconds; when Binance cannot be reached, stats_7d is null, stats_7d_error "
        "says why, and the rest of the response is still returned with 200. "
        "The symbol may be written as BTCEUR, btceur, BTC-EUR or BTC/EUR."
    ),
    responses={404: UNKNOWN_SYMBOL},
)
async def get_pair_summary(
    symbol: str,
    session: Session,
    service: MarketService,
    orderbooks: OrderBooks,
    precision: Precision,
    pair_stats: PairStats,
    alerts: Alerts,
) -> PairSummary:
    normalized = normalize_symbol(symbol, service.settings.supported_symbols)
    market = next(m for m in service.snapshot()["markets"] if m["symbol"] == normalized)

    stats_7d: WindowStats | None = None
    stats_7d_error: str | None = None
    try:
        stats_7d = await pair_stats.get_7d(normalized)
    except ExchangeUnavailableError as exc:
        # The statistics are one part of the page; losing them must not lose the rest.
        logger.warning("7-day statistics for %s unavailable: %s", normalized, exc)
        stats_7d_error = STATS_UNAVAILABLE

    meta = await pair_meta_service.pair_meta(session, normalized)
    chart_notes_count = await session.scalar(
        select(func.count()).select_from(Note).where(Note.symbol == normalized)
    )
    return PairSummary(
        market=to_market_view(market, service.settings.spread_warning_percent),
        stats_7d=stats_7d,
        stats_7d_error=stats_7d_error,
        meta=next(m for m in precision.view() if m.symbol == normalized),
        orderbook=orderbooks.view(normalized),
        tags=meta.tags,
        pair_note=meta.note,
        active_alerts=[
            AlertOut.model_validate(alert)
            for alert in await alerts.list_alerts(active=True, symbol=normalized)
        ],
        triggered_alerts=[
            AlertOut.model_validate(alert)
            for alert in await alerts.recent_triggered(normalized, TRIGGERED_ALERTS_LIMIT)
        ],
        chart_notes_count=chart_notes_count or 0,
    )


@pairs_router.put(
    "/{symbol}/tags",
    response_model=PairMeta,
    dependencies=WriteGuards,
    summary="Replace the tags of a pair",
    description=(
        f"Sets the pair's whole set of tags (at most {MAX_TAGS_PER_PAIR}); duplicate ids are "
        "ignored. Every browser receives a pair_meta_updated message."
    ),
    responses={
        400: error_response(f"More than {MAX_TAGS_PER_PAIR} different tags.", TOO_MANY_PAIR_TAGS),
        403: WRITE_FORBIDDEN,
        404: UNKNOWN_SYMBOL,
        422: validation_or_error("tag_ids is not a list of integers, or an id does not exist."),
        429: RATE_LIMITED,
    },
)
async def set_pair_tags(
    symbol: str,
    payload: PairTagsUpdate,
    session: Session,
    service: MarketService,
    manager: Manager,
) -> PairMeta:
    normalized = normalize_symbol(symbol, service.settings.supported_symbols)
    try:
        await pair_meta_service.set_pair_tags(session, normalized, payload.tag_ids)
    except PairTagLimitError as exc:
        raise HTTPException(status_code=400, detail=TOO_MANY_PAIR_TAGS) from exc
    except UnknownTagError as exc:
        ids = ", ".join(str(tag_id) for tag_id in exc.tag_ids)
        raise HTTPException(status_code=422, detail=f"Oznaka ne postoji: {ids}") from exc
    meta = await pair_meta_service.pair_meta(session, normalized)
    await broadcast_pair_meta(manager, meta)
    return meta


@pairs_router.put(
    "/{symbol}/note",
    response_model=PairMeta,
    dependencies=WriteGuards,
    summary="Set the note of a pair",
    description=(
        "Creates or replaces the pair's note; empty text deletes it. Every browser receives "
        "a pair_meta_updated message."
    ),
    responses={
        403: WRITE_FORBIDDEN,
        404: UNKNOWN_SYMBOL,
        422: validation_error("Missing or too long text."),
        429: RATE_LIMITED,
    },
)
async def set_pair_note(
    symbol: str,
    payload: PairNoteUpdate,
    session: Session,
    service: MarketService,
    manager: Manager,
) -> PairMeta:
    normalized = normalize_symbol(symbol, service.settings.supported_symbols)
    await pair_meta_service.set_pair_note(session, normalized, payload.text)
    meta = await pair_meta_service.pair_meta(session, normalized)
    await broadcast_pair_meta(manager, meta)
    return meta
