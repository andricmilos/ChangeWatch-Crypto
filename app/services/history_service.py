import asyncio
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, localcontext
from typing import Any

from app.schemas.history import (
    CurrentPriceType,
    HistoricalValue,
    HistoricalValueResponse,
    HistoricalValueResult,
    Period,
)
from app.schemas.market import KlineUpdate, MarketTicker
from app.services.converter import CRYPTO_STEP, EUR_STEP, PRECISION
from app.services.exchange_client import BinanceRestClient

HISTORY_INTERVAL = "1h"
MAX_LOOKBACK = timedelta(days=365)
PERIODS: dict[Period, timedelta] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
    "1y": MAX_LOOKBACK,
}
DEFAULT_CACHE_ENTRIES = 1000
NO_CANDLE_REASON = "nema podataka za taj datum"
NO_CURRENT_PRICE_REASON = "nema trenutne cene"


class InvalidMomentError(ValueError):
    """The requested moment is missing, ambiguous, in the future or too far back."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def hour_start(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def current_price(ticker: MarketTicker) -> tuple[CurrentPriceType, Decimal]:
    # Selling today gets the bid; without an order book the last trade is the best estimate.
    if ticker.bid_price is not None:
        return "bid", ticker.bid_price
    return "last", ticker.last_price


def _round_two_places(value: Decimal) -> Decimal:
    rounded = value.quantize(EUR_STEP, rounding=ROUND_HALF_UP)
    # A tiny loss such as -0.000001 would otherwise be shown as "-0.00".
    return rounded.copy_abs() if rounded.is_zero() else rounded


def compute_value(
    amount: Decimal, candle: KlineUpdate, market: dict[str, Any]
) -> HistoricalValueResult:
    ticker: MarketTicker = market["ticker"]
    price_type, price = current_price(ticker)
    with localcontext() as context:
        context.prec = PRECISION
        quantity = (amount / candle.open).quantize(CRYPTO_STEP, rounding=ROUND_DOWN)
        # Profit is computed from the unrounded value, so it is not off by a rounded cent.
        value = quantity * price
        profit = value - amount
        return HistoricalValueResult(
            historical_price=candle.open,
            candle_open_time=candle.open_time,
            quantity=quantity,
            current_price=price,
            current_price_type=price_type,
            current_price_updated_at=market["updated_at"],
            freshness=market["freshness"],
            value_eur=_round_two_places(value),
            profit_eur=_round_two_places(profit),
            profit_percent=_round_two_places(profit / amount * 100),
        )


class HistoryService:
    """What an EUR amount bought at a past hour would be worth today, per pair."""

    def __init__(
        self,
        rest_client: BinanceRestClient,
        clock: Callable[[], datetime] = utc_now,
        max_cache_entries: int = DEFAULT_CACHE_ENTRIES,
    ) -> None:
        self.rest_client = rest_client
        self.clock = clock
        self.max_cache_entries = max_cache_entries
        # (symbol, hour start) -> candle, or None when Binance has no candle for that hour.
        # Past candles never change, so entries only leave when the cache is full (LRU).
        self._cache: OrderedDict[tuple[str, datetime], KlineUpdate | None] = OrderedDict()

    def resolve_moment(self, period: Period | None, at: datetime | None) -> datetime:
        now = self.clock()
        if period is not None and at is None:
            return now - PERIODS[period]
        if period is not None or at is None:
            raise InvalidMomentError("Zadaj tačno jedno od: period ili at")

        if at.tzinfo is None:
            raise InvalidMomentError("Vreme mora imati vremensku zonu, npr. 2026-09-18T12:00:00Z")
        if at >= now:
            raise InvalidMomentError("Vreme mora biti u prošlosti")
        if at < now - MAX_LOOKBACK:
            raise InvalidMomentError("Vreme može biti najviše 1 godinu unazad")
        return at.astimezone(UTC)

    async def value_over_time(
        self,
        amount: Decimal,
        moment: datetime,
        period: Period | None,
        markets: list[dict[str, Any]],
    ) -> HistoricalValueResponse:
        start = hour_start(moment)
        # ExchangeUnavailableError propagates and becomes a 503 in main.py.
        candles = await asyncio.gather(
            *(self._candle_at(market["symbol"], start) for market in markets)
        )
        return HistoricalValueResponse(
            amount=amount,
            period=period,
            requested_at=moment,
            candle_open_time=start,
            results=[
                self._result(amount, market, candle)
                for market, candle in zip(markets, candles, strict=True)
            ],
        )

    def _result(
        self, amount: Decimal, market: dict[str, Any], candle: KlineUpdate | None
    ) -> HistoricalValue:
        item = HistoricalValue(symbol=market["symbol"], display_symbol=market["display_symbol"])
        if candle is None:
            return item.model_copy(update={"reason": NO_CANDLE_REASON})
        if market["ticker"] is None:
            return item.model_copy(update={"reason": NO_CURRENT_PRICE_REASON})
        return item.model_copy(update={"result": compute_value(amount, candle, market)})

    async def _candle_at(self, symbol: str, start: datetime) -> KlineUpdate | None:
        key = (symbol, start)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]

        candle = await self.rest_client.get_kline_at(symbol, HISTORY_INTERVAL, start)
        # Binance returns the first candle at or after startTime; a later one means the
        # pair did not trade in that hour (e.g. it was not listed yet).
        if candle is not None and candle.open_time != start:
            candle = None

        self._cache[key] = candle
        if len(self._cache) > self.max_cache_entries:
            self._cache.popitem(last=False)
        return candle
