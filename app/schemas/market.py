from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


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
    # "ws" for the live stream, "rest" for the fallback polling while the stream is down.
    source: Literal["ws", "rest"] = "ws"


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


FRESHNESS_DESCRIPTION = (
    "live: fresh data from Binance; stale: no message for a while; no_data: nothing received yet."
)


class MarketView(BaseModel):
    """One market as the API shows it; numbers stay null until real data arrives."""

    symbol: str = Field(description="Pair in Binance format.", examples=["BTCEUR"])
    display_symbol: str = Field(description="Pair for display.", examples=["BTC/EUR"])
    last_price: Decimal | None = Field(
        default=None, description="Last trade price in EUR.", examples=["72309.76"]
    )
    price_change: Decimal | None = Field(
        default=None, description="Absolute price change over 24 hours.", examples=["-812.44"]
    )
    price_change_percent: Decimal | None = Field(
        default=None, description="Price change over 24 hours, in percent.", examples=["-1.111"]
    )
    change_1h_percent: Decimal | None = Field(
        default=None, description="Price change over the last hour, in percent.", examples=["0.245"]
    )
    change_4h_percent: Decimal | None = Field(
        default=None,
        description="Price change over the last 4 hours, in percent.",
        examples=["-0.532"],
    )
    high_24h: Decimal | None = Field(
        default=None, description="Highest price in the last 24 hours.", examples=["73480.00"]
    )
    low_24h: Decimal | None = Field(
        default=None, description="Lowest price in the last 24 hours.", examples=["71655.12"]
    )
    volume_24h: Decimal | None = Field(
        default=None,
        description="Traded volume in the last 24 hours, in the base asset.",
        examples=["412.83920000"],
    )
    bid_price: Decimal | None = Field(
        default=None, description="Best bid (buy) price.", examples=["72309.75"]
    )
    ask_price: Decimal | None = Field(
        default=None, description="Best ask (sell) price.", examples=["72309.76"]
    )
    spread: Decimal | None = Field(
        default=None, description="ask_price - bid_price.", examples=["0.01"]
    )
    spread_percent: Decimal | None = Field(
        default=None,
        description="Spread relative to the mid price, in percent (3 decimals).",
        examples=["0.000"],
    )
    # True when spread_percent is above SPREAD_WARNING_PERCENT; null without bid/ask.
    high_spread: bool | None = Field(
        default=None,
        description="True when the spread is above the configured warning threshold.",
        examples=[False],
    )
    updated_at: datetime | None = Field(
        default=None,
        description="Time of the last 24h ticker from Binance.",
        examples=["2026-10-21T14:05:12.345Z"],
    )
    freshness: str = Field(description=FRESHNESS_DESCRIPTION, examples=["live"])


class CandleOut(BaseModel):
    """One candle for the chart; time is in seconds because Lightweight Charts expects that."""

    time: int = Field(description="Candle open time, Unix seconds (UTC).", examples=[1792591500])
    open: Decimal = Field(description="Open price.", examples=["72290.01"])
    high: Decimal = Field(description="Highest price.", examples=["72330.00"])
    low: Decimal = Field(description="Lowest price.", examples=["72281.15"])
    close: Decimal = Field(description="Close price.", examples=["72309.76"])
    volume: Decimal = Field(description="Volume in the base asset.", examples=["1.84321000"])


class MarketsResponse(BaseModel):
    connection_status: str = Field(
        description=(
            "State of the server's WebSocket connection to Binance: "
            "connecting, connected, reconnecting or stopped."
        ),
        examples=["connected"],
    )
    price_source: str = Field(
        default="ws",
        description=(
            "Where prices come from: ws (live stream) or rest (polling Binance REST every few "
            "seconds while the stream is down)."
        ),
        examples=["ws"],
    )
    markets: list[MarketView] = Field(description="Every supported pair, in configured order.")
