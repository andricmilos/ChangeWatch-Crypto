from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

PriceType = Literal["ask", "bid", "last"]


class ConversionResult(BaseModel):
    """One target currency; amount and price stay null when the pair has no data, never 0."""

    currency: str = Field(description="Target currency.", examples=["BTC"])
    # The pair whose price produced this result, e.g. "ETHEUR" for EUR -> ETH.
    symbol: str = Field(description="Pair whose price was used.", examples=["BTCEUR"])
    amount: Decimal | None = Field(
        default=None,
        description="Converted amount in the target currency.",
        examples=["0.01382940"],
    )
    price_type: PriceType | None = Field(
        default=None,
        description="Which price was used: ask when buying, bid when selling, last as a fallback.",
        examples=["ask"],
    )
    price: Decimal | None = Field(
        default=None, description="Price used for the conversion, in EUR.", examples=["72309.76"]
    )
    updated_at: datetime | None = Field(
        default=None,
        description="When that price was received.",
        examples=["2026-10-21T14:05:12.345Z"],
    )
    freshness: str = Field(
        description="live, stale or no_data; the worse of the pairs involved.", examples=["live"]
    )


class ConversionResponse(BaseModel):
    amount: Decimal = Field(description="Requested amount.", examples=["1000"])
    currency: str = Field(description="Currency of the requested amount.", examples=["EUR"])
    results: list[ConversionResult] = Field(
        description="EUR converts to every supported asset; an asset converts to EUR."
    )
