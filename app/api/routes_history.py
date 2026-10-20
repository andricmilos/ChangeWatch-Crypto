from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import get_history_service, get_market_service, normalize_symbol
from app.api.rate_limit import history_limiter
from app.api.routes_convert import Amount
from app.schemas.history import HistoricalValueResponse, Period
from app.services.history_service import HistoryService, InvalidMomentError
from app.services.market_data_service import MarketDataService

router = APIRouter(prefix="/api/history", tags=["history"])

MarketService = Annotated[MarketDataService, Depends(get_market_service)]
History = Annotated[HistoryService, Depends(get_history_service)]


# Rate limited because every uncached request calls Binance REST.
@router.get(
    "/value", response_model=HistoricalValueResponse, dependencies=[Depends(history_limiter)]
)
async def historical_value(
    service: MarketService,
    history: History,
    amount: Amount,
    period: Period | None = None,
    at: datetime | None = None,
    symbol: str | None = None,
) -> HistoricalValueResponse:
    try:
        moment = history.resolve_moment(period, at)
    except InvalidMomentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    markets = service.snapshot()["markets"]
    if symbol is not None:
        normalized = normalize_symbol(symbol, service.settings.supported_symbols)
        markets = [market for market in markets if market["symbol"] == normalized]
    return await history.value_over_time(amount, moment, period, markets)
