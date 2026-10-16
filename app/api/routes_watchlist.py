from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_market_service, normalize_symbol
from app.api.rate_limit import write_limiter
from app.database import get_session
from app.models import WatchlistItem
from app.schemas.watchlist import WatchlistCreate, WatchlistItemOut
from app.services.market_data_service import MarketDataService

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])

Session = Annotated[AsyncSession, Depends(get_session)]
MarketService = Annotated[MarketDataService, Depends(get_market_service)]
RateLimited = [Depends(write_limiter)]


@router.get("", response_model=list[WatchlistItemOut])
async def list_watchlist(session: Session) -> list[WatchlistItem]:
    # id grows with every insert, so it is the order in which pairs were added.
    result = await session.scalars(select(WatchlistItem).order_by(WatchlistItem.id))
    return list(result)


@router.post("", response_model=WatchlistItemOut, status_code=201, dependencies=RateLimited)
async def add_to_watchlist(payload: WatchlistCreate, session: Session) -> WatchlistItem:
    item = WatchlistItem(symbol=payload.symbol)
    session.add(item)
    try:
        await session.commit()
    except IntegrityError as exc:
        # The unique constraint, not a prior SELECT, decides: a check in code could
        # pass for two simultaneous requests, the database never lets both through.
        await session.rollback()
        raise HTTPException(status_code=409, detail="Par je već u watchlist-i") from exc
    return item


@router.delete("/{symbol}", status_code=204, dependencies=RateLimited)
async def remove_from_watchlist(symbol: str, session: Session, service: MarketService) -> Response:
    normalized = normalize_symbol(symbol, service.settings.supported_symbols)
    result = await session.execute(delete(WatchlistItem).where(WatchlistItem.symbol == normalized))
    await session.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Par nije u watchlist-i")
    return Response(status_code=204)
