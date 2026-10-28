from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.dependencies import get_candle_service, get_market_service, normalize_symbol
from app.schemas.error import EXCHANGE_UNAVAILABLE, UNKNOWN_SYMBOL, validation_or_error
from app.schemas.market import CandleOut, MarketsResponse, MarketView
from app.services.candle_service import CandleService
from app.services.exchange_client import datetime_to_ms
from app.services.market_data_service import MarketDataService

router = APIRouter(prefix="/api/markets", tags=["markets"])

MarketService = Annotated[MarketDataService, Depends(get_market_service)]
Candles = Annotated[CandleService, Depends(get_candle_service)]


def to_market_view(market: dict[str, Any], spread_warning_percent: Decimal) -> MarketView:
    ticker = market["ticker"]
    view = MarketView(
        symbol=market["symbol"],
        display_symbol=market["display_symbol"],
        # Rolling changes come from their own streams, so they can exist before the 24h ticker.
        change_1h_percent=market["change_1h_percent"],
        change_4h_percent=market["change_4h_percent"],
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
            "bid_price": ticker.bid_price,
            "ask_price": ticker.ask_price,
            "spread": ticker.spread,
            "spread_percent": ticker.spread_percent,
            "high_spread": (
                None
                if ticker.spread_percent is None
                else ticker.spread_percent > spread_warning_percent
            ),
        }
    )


@router.get(
    "",
    response_model=MarketsResponse,
    summary="List all markets",
    description=(
        "Latest prices, 24h statistics and spread for every supported pair, from the live "
        "Binance stream. Prices are null until the first ticker arrives."
    ),
)
async def list_markets(service: MarketService) -> MarketsResponse:
    snapshot = service.snapshot()
    return MarketsResponse(
        connection_status=snapshot["connection_status"],
        price_source=snapshot["price_source"],
        markets=[
            to_market_view(market, service.settings.spread_warning_percent)
            for market in snapshot["markets"]
        ],
    )


@router.get(
    "/{symbol}",
    response_model=MarketView,
    summary="Get one market",
    description=(
        "Same data as the market list, for one pair. "
        "The symbol may be written as BTCEUR, btceur, BTC-EUR or BTC/EUR."
    ),
    responses={404: UNKNOWN_SYMBOL},
)
async def get_market(symbol: str, service: MarketService) -> MarketView:
    normalized = normalize_symbol(symbol, service.settings.supported_symbols)
    market = next(m for m in service.snapshot()["markets"] if m["symbol"] == normalized)
    return to_market_view(market, service.settings.spread_warning_percent)


@router.get(
    "/{symbol}/candles",
    response_model=list[CandleOut],
    summary="Get candles for the chart",
    description=(
        "OHLCV candles from Binance, oldest first, cached for 15 seconds. When Binance is "
        "unreachable, 1m candles come from the local database; other intervals return 503."
    ),
    responses={
        404: UNKNOWN_SYMBOL,
        422: validation_or_error("Unsupported interval, or limit outside 1–1000."),
        503: EXCHANGE_UNAVAILABLE,
    },
)
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

    # ExchangeUnavailableError becomes a 503 in the global handler in main.py.
    candles = await candle_service.get_candles(normalized, interval, limit)
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
