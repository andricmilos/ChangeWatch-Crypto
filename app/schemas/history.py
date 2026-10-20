from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

Period = Literal["24h", "7d", "30d", "90d", "1y"]
CurrentPriceType = Literal["bid", "last"]


class HistoricalValueResult(BaseModel):
    # Open price of the 1h candle that contains the requested moment.
    historical_price: Decimal
    candle_open_time: datetime
    # Bought for the full EUR amount at the historical price, 8 decimals rounded down.
    quantity: Decimal
    current_price: Decimal
    current_price_type: CurrentPriceType
    current_price_updated_at: datetime | None
    freshness: str
    value_eur: Decimal
    profit_eur: Decimal
    profit_percent: Decimal


class HistoricalValue(BaseModel):
    """One pair; `result` is null with a `reason` when it cannot be computed, never 0."""

    symbol: str
    display_symbol: str
    result: HistoricalValueResult | None = None
    reason: str | None = None


class HistoricalValueResponse(BaseModel):
    amount: Decimal
    period: Period | None
    requested_at: datetime
    # Every pair uses the candle of the same hour; this is when it opened.
    candle_open_time: datetime
    results: list[HistoricalValue]
