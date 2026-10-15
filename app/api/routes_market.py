from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.dependencies import get_candle_service, get_market_service, normalize_symbol
from app.schemas.market import CandleOut, MarketsResponse, MarketView
from app.services.candle_service import CandleService
from app.services.exchange_client import ExchangeUnavailableError, datetime_to_ms
from app.services.market_data_service import MarketDataService

router = APIRouter(prefix="/api/markets", tags=["markets"])

MarketService = Annotated[MarketDataService, Depends(get_market_service)]
Candles = Annotated[CandleService, Depends(get_candle_service)]


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


@router.get("/{symbol}/candles", response_model=list[CandleOut])
async def get_candles(
    symbol: str,
    service: MarketService,
    candle_service: Candles,
    interval: str = "1m",
    limit: Annotated[int, Query(ge=1, le=1000)] = 300,
) -> list[CandleOut]:
    normalized = normalize_symbol(symbol, service.settings.supported_symbols)
    allowed_intervals = service.settings.kline_intervals
    if interval not in allowed_intervals:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported interval {interval!r}; use one of {allowed_intervals}",
        )

    try:
        candles = await candle_service.get_candles(normalized, interval, limit)
    except ExchangeUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Exchange data is unavailable") from exc

    return [
        CandleOut(
            time=datetime_to_ms(candle.open_time) // 1000,
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            volume=candle.volume,
        )
        for candle in candles
    ]
