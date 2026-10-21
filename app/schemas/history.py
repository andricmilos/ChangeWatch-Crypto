from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

Period = Literal["24h", "7d", "30d", "90d", "1y"]
CurrentPriceType = Literal["bid", "last"]


class HistoricalValueResult(BaseModel):
    # Open price of the 1h candle that contains the requested moment.
    historical_price: Decimal = Field(
        description="Open price of the 1h candle that contains the requested moment.",
        examples=["61250.00"],
    )
    candle_open_time: datetime = Field(
        description="Open time of that candle.", examples=["2026-09-21T14:00:00Z"]
    )
    # Bought for the full EUR amount at the historical price, 8 decimals rounded down.
    quantity: Decimal = Field(
        description="Amount bought for the full EUR sum, 8 decimals rounded down.",
        examples=["0.01632653"],
    )
    current_price: Decimal = Field(description="Current price in EUR.", examples=["72309.75"])
    current_price_type: CurrentPriceType = Field(
        description="bid when available (the price you could sell at), otherwise last.",
        examples=["bid"],
    )
    current_price_updated_at: datetime | None = Field(
        description="When the current price was received.", examples=["2026-10-21T14:05:12.345Z"]
    )
    freshness: str = Field(description="live, stale or no_data.", examples=["live"])
    value_eur: Decimal = Field(description="quantity × current_price.", examples=["1180.56"])
    profit_eur: Decimal = Field(description="value_eur − amount.", examples=["180.56"])
    profit_percent: Decimal = Field(description="Profit in percent.", examples=["18.06"])


class HistoricalValue(BaseModel):
    """One pair; `result` is null with a `reason` when it cannot be computed, never 0."""

    symbol: str = Field(description="Pair in Binance format.", examples=["BTCEUR"])
    display_symbol: str = Field(description="Pair for display.", examples=["BTC/EUR"])
    result: HistoricalValueResult | None = None
    reason: str | None = Field(
        default=None,
        description="Why there is no result, e.g. no candle for that time.",
        examples=["nema podataka za taj datum"],
    )


class HistoricalValueResponse(BaseModel):
    amount: Decimal = Field(description="Invested amount in EUR.", examples=["1000"])
    period: Period | None = Field(description="Requested period, if any.", examples=["30d"])
    requested_at: datetime = Field(
        description="The moment the amount would have been invested.",
        examples=["2026-09-21T14:05:12Z"],
    )
    # Every pair uses the candle of the same hour; this is when it opened.
    candle_open_time: datetime = Field(
        description="Open time of the 1h candle used for every pair.",
        examples=["2026-09-21T14:00:00Z"],
    )
    results: list[HistoricalValue] = Field(description="One entry per pair.")
