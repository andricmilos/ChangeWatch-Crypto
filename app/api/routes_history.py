from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import get_history_service, get_market_service, normalize_symbol
from app.api.rate_limit import history_limiter
from app.api.routes_convert import Amount
from app.schemas.error import (
    EXCHANGE_UNAVAILABLE,
    RATE_LIMITED,
    UNKNOWN_SYMBOL,
    validation_or_error,
)
from app.schemas.history import HistoricalValueResponse, Period
from app.services.history_service import HistoryService, InvalidMomentError
from app.services.market_data_service import MarketDataService

router = APIRouter(prefix="/api/history", tags=["history"])

MarketService = Annotated[MarketDataService, Depends(get_market_service)]
History = Annotated[HistoryService, Depends(get_history_service)]


# Rate limited because every uncached request calls Binance REST.
@router.get(
    "/value",
    response_model=HistoricalValueResponse,
    dependencies=[Depends(history_limiter)],
    summary="Value today of an amount invested in the past",
    description=(
        "What a EUR amount invested at a past moment (period or at, exactly one) would be "
        "worth now, per pair, using hourly Binance candles. Limited to 20 requests per minute."
    ),
    responses={
        404: UNKNOWN_SYMBOL,
        422: validation_or_error(
            "Invalid amount or period, both or neither of period and at, or at without a "
            "time zone, in the future or more than 1 year back."
        ),
        429: RATE_LIMITED,
        503: EXCHANGE_UNAVAILABLE,
    },
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
