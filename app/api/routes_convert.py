from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.dependencies import get_market_service
from app.schemas.convert import ConversionResponse
from app.schemas.error import validation_or_error
from app.services.converter import EUR, base_assets, convert
from app.services.market_data_service import MarketDataService

router = APIRouter(prefix="/api/convert", tags=["convert"])

MarketService = Annotated[MarketDataService, Depends(get_market_service)]
# allow_inf_nan is off by default for Decimal, so "NaN" and "Infinity" are rejected too.
Amount = Annotated[Decimal, Query(gt=0, le=1_000_000_000, decimal_places=8)]


@router.get(
    "",
    response_model=ConversionResponse,
    summary="Convert an amount at live prices",
    description=(
        "Converts EUR into every supported asset (at the ask price), or an asset into EUR "
        "(at the bid price), using the latest prices. Results without data stay null."
    ),
    responses={
        422: validation_or_error(
            "Amount not greater than 0, above 1,000,000,000 or with more than 8 decimals, "
            "or an unsupported currency."
        )
    },
)
async def convert_amount(
    service: MarketService, amount: Amount, currency: str = EUR
) -> ConversionResponse:
    allowed = [EUR, *base_assets(service.settings.supported_symbols)]
    normalized = currency.strip().upper()
    if normalized not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported currency {currency!r}; use one of {allowed}",
        )
    return convert(amount, normalized, service.snapshot()["markets"])
