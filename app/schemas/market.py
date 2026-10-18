from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class MarketTicker(BaseModel):
    symbol: str
    display_symbol: str
    last_price: Decimal
    price_change: Decimal | None = None
    price_change_percent: Decimal | None = None
    high_24h: Decimal | None = None
    low_24h: Decimal | None = None
    volume_24h: Decimal | None = None
    # Best bid and ask at the time of the ticker ("b" and "a"); the converter prefers them.
    bid_price: Decimal | None = None
    ask_price: Decimal | None = None
    # ask - bid, and the same relative to the mid price in percent (3 decimals).
    spread: Decimal | None = None
    spread_percent: Decimal | None = None
    event_time: datetime


class RollingChange(BaseModel):
    """Percent change over a rolling window ("1h" or "4h") from a Binance <window>Ticker."""

    symbol: str
    window: str
    change_percent: Decimal
    event_time: datetime


class KlineUpdate(BaseModel):
    symbol: str
    interval: str
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    is_closed: bool


class MarketView(BaseModel):
    """One market as the API shows it; numbers stay null until real data arrives."""

    symbol: str
    display_symbol: str
    last_price: Decimal | None = None
    price_change: Decimal | None = None
    price_change_percent: Decimal | None = None
    change_1h_percent: Decimal | None = None
    change_4h_percent: Decimal | None = None
    high_24h: Decimal | None = None
    low_24h: Decimal | None = None
    volume_24h: Decimal | None = None
    bid_price: Decimal | None = None
    ask_price: Decimal | None = None
    spread: Decimal | None = None
    spread_percent: Decimal | None = None
    # True when spread_percent is above SPREAD_WARNING_PERCENT; null without bid/ask.
    high_spread: bool | None = None
    updated_at: datetime | None = None
    freshness: str


class CandleOut(BaseModel):
    """One candle for the chart; time is in seconds because Lightweight Charts expects that."""

    time: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class MarketsResponse(BaseModel):
    connection_status: str
    markets: list[MarketView]
