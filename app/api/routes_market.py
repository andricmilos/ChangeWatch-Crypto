from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.api.dependencies import get_market_service, normalize_symbol
from app.schemas.market import MarketsResponse, MarketView
from app.services.market_data_service import MarketDataService

router = APIRouter(prefix="/api/markets", tags=["markets"])

MarketService = Annotated[MarketDataService, Depends(get_market_service)]


def to_market_view(market: dict[str, Any]) -> MarketView:
    ticker = market["ticker"]
    view = MarketView(
        symbol=market["symbol"],
        display_symbol=market["display_symbol"],
        updated_at=market["updated_at"],
        freshness=market["freshness"],
    )
    if ticker is None:
        # No data yet: prices stay null, never 0.
        return view
    return view.model_copy(
        update={
            "last_price": ticker.last_price,
            "price_change": ticker.price_change,
            "price_change_percent": ticker.price_change_percent,
            "high_24h": ticker.high_24h,
            "low_24h": ticker.low_24h,
            "volume_24h": ticker.volume_24h,
        }
    )


@router.get("", response_model=MarketsResponse)
async def list_markets(service: MarketService) -> MarketsResponse:
    snapshot = service.snapshot()
    return MarketsResponse(
        connection_status=snapshot["connection_status"],
        markets=[to_market_view(market) for market in snapshot["markets"]],
    )


@router.get("/{symbol}", response_model=MarketView)
async def get_market(symbol: str, service: MarketService) -> MarketView:
    normalized = normalize_symbol(symbol, service.settings.supported_symbols)
    market = next(m for m in service.snapshot()["markets"] if m["symbol"] == normalized)
    return to_market_view(market)
