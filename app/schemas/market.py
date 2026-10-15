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
    high_24h: Decimal | None = None
    low_24h: Decimal | None = None
    volume_24h: Decimal | None = None
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
