from pydantic import BaseModel, Field

from app.schemas.alert import AlertOut
from app.schemas.market import MarketMeta, MarketView, WindowStats
from app.schemas.orderbook import OrderBookView
from app.schemas.tag import PairNoteOut, TagOut


class PairSummary(BaseModel):
    """Everything about one pair for the pair page, in a single response."""

    market: MarketView = Field(description="Live price, 24h statistics, bid/ask and freshness.")
    stats_7d: WindowStats | None = Field(
        description="Statistics over the last 7 days; null when Binance could not be reached."
    )
    stats_7d_error: str | None = Field(
        description="Why stats_7d is null; null when it loaded.", examples=[None]
    )
    meta: MarketMeta = Field(description="Price and quantity decimals for display.")
    orderbook: OrderBookView = Field(description="Top of the live order book.")
    tags: list[TagOut] = Field(description="Tags of the pair, sorted by name.")
    pair_note: PairNoteOut | None = Field(description="Note about the pair, or null.")
    active_alerts: list[AlertOut] = Field(description="Active alerts of the pair, newest first.")
    triggered_alerts: list[AlertOut] = Field(
        description="The last 10 fired alerts of the pair, newest first."
    )
    chart_notes_count: int = Field(description="Number of notes on the pair's chart.", examples=[3])
