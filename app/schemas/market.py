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
