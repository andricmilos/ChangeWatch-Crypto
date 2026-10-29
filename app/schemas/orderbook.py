"""Order book (top bids and asks) from the Binance Partial Book Depth stream."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class OrderBookLevel(BaseModel):
    price: Decimal = Field(description="Price of this level in the quote currency.")
    quantity: Decimal = Field(description="Quantity offered at this price, in the base asset.")
    cumulative_quantity: Decimal = Field(
        description="Quantity of this level and every better one on the same side."
    )


class OrderBookSnapshot(BaseModel):
    """The latest depth message of one pair; every derived field is computed on parsing."""

    symbol: str
    last_update_id: int
    # Binance sends no event time with depth messages, so this is when the server received it.
    received_at: datetime
    # Best price first: bids descending, asks ascending.
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    best_bid: Decimal
    best_ask: Decimal
    spread: Decimal
    # Spread relative to the mid price, in percent (3 decimals).
    spread_percent: Decimal
    mid_price: Decimal
    bid_volume: Decimal
    ask_volume: Decimal
    # (bid_volume - ask_volume) / (bid_volume + ask_volume) * 100, 2 decimals; None when both
    # volumes are 0, because the ratio is undefined then.
    imbalance_percent: Decimal | None


class OrderBookView(BaseModel):
    """GET /api/markets/{symbol}/orderbook; numbers stay null and lists empty without data."""

    symbol: str = Field(description="Pair in Binance format.", examples=["BTCEUR"])
    display_symbol: str = Field(description="Pair for display.", examples=["BTC/EUR"])
    freshness: str = Field(
        description=(
            "live: a depth message arrived within ORDERBOOK_STALE_SECONDS; stale: the last one "
            "is older; no_data: nothing received yet."
        ),
        examples=["live"],
    )
    last_update_id: int | None = Field(
        default=None, description="Binance book update id of this snapshot.", examples=[160]
    )
    received_at: datetime | None = Field(
        default=None,
        description="When the server received the snapshot (UTC); Binance sends no time.",
        examples=["2026-10-21T14:05:12.345Z"],
    )
    bids: list[OrderBookLevel] = Field(
        default_factory=list, description="Buy orders, best (highest) price first."
    )
    asks: list[OrderBookLevel] = Field(
        default_factory=list, description="Sell orders, best (lowest) price first."
    )
    best_bid: Decimal | None = Field(default=None, examples=["72309.75"])
    best_ask: Decimal | None = Field(default=None, examples=["72309.76"])
    spread: Decimal | None = Field(
        default=None, description="best_ask - best_bid.", examples=["0.01"]
    )
    spread_percent: Decimal | None = Field(
        default=None,
        description="Spread relative to the mid price, in percent (3 decimals).",
        examples=["0.000"],
    )
    mid_price: Decimal | None = Field(
        default=None, description="(best_bid + best_ask) / 2.", examples=["72309.755"]
    )
    bid_volume: Decimal | None = Field(
        default=None, description="Sum of all bid quantities shown.", examples=["3.21"]
    )
    ask_volume: Decimal | None = Field(
        default=None, description="Sum of all ask quantities shown.", examples=["1.87"]
    )
    imbalance_percent: Decimal | None = Field(
        default=None,
        description=(
            "(bid_volume - ask_volume) / (bid_volume + ask_volume) * 100, 2 decimals; "
            "positive means more buy volume."
        ),
        examples=["26.42"],
    )
