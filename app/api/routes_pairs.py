from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_connection_manager, get_market_service, normalize_symbol
from app.api.routes_notes import WRITE_FORBIDDEN, WriteGuards
from app.database import get_session
from app.models import Tag
from app.schemas.error import (
    RATE_LIMITED,
    UNKNOWN_SYMBOL,
    error_response,
    validation_error,
    validation_or_error,
)
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
from app.services.market_data_service import MarketDataService
from app.services.pair_meta_service import (
    MAX_TAGS,
    MAX_TAGS_PER_PAIR,
    DuplicateTagNameError,
    PairTagLimitError,
    TagLimitError,
    UnknownTagError,
)
from app.websocket.manager import ConnectionManager

tags_router = APIRouter(prefix="/api/tags", tags=["tags"])
pairs_router = APIRouter(prefix="/api/pairs", tags=["pairs"])

Session = Annotated[AsyncSession, Depends(get_session)]
MarketService = Annotated[MarketDataService, Depends(get_market_service)]
Manager = Annotated[ConnectionManager, Depends(get_connection_manager)]

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
