from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

PriceType = Literal["ask", "bid", "last"]


class ConversionResult(BaseModel):
    """One target currency; amount and price stay null when the pair has no data, never 0."""

    currency: str
    # The pair whose price produced this result, e.g. "ETHEUR" for EUR -> ETH.
    symbol: str
    amount: Decimal | None = None
    price_type: PriceType | None = None
    price: Decimal | None = None
    updated_at: datetime | None = None
    freshness: str


class ConversionResponse(BaseModel):
    amount: Decimal
    currency: str
    results: list[ConversionResult]
