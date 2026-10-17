import logging
import time
from collections.abc import Callable, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Candle
from app.schemas.market import KlineUpdate
from app.services.exchange_client import (
    BinanceRestClient,
    ExchangeUnavailableError,
    datetime_to_ms,
    ms_to_datetime,
)
from app.services.market_data_service import MarketEvent
from app.services.runtime_stats import RuntimeStats

logger = logging.getLogger(__name__)

STORED_INTERVAL = "1m"
PRICE_COLUMNS = ("open_price", "high_price", "low_price", "close_price", "volume")


def _to_row(candle: KlineUpdate) -> dict[str, object]:
    return {
        "symbol": candle.symbol,
        "interval": candle.interval,
        "open_time": datetime_to_ms(candle.open_time),
        "close_time": datetime_to_ms(candle.close_time),
        "open_price": candle.open,
        "high_price": candle.high,
        "low_price": candle.low,
        "close_price": candle.close,
        "volume": candle.volume,
    }


def _from_row(row: Candle) -> KlineUpdate:
    return KlineUpdate(
        symbol=row.symbol,
        interval=row.interval,
        open_time=ms_to_datetime(row.open_time),
        close_time=ms_to_datetime(row.close_time),
        open=row.open_price,
        high=row.high_price,
        low=row.low_price,
        close=row.close_price,
        volume=row.volume,
        # Only closed candles are ever written to the database.
        is_closed=True,
    )


class CandleService:
    """Historical candles: Binance REST first, SQLite as the local copy and 1m fallback."""

    def __init__(
        self,
        rest_client: BinanceRestClient,
        session_factory: async_sessionmaker[AsyncSession],
        cache_seconds: float = 15,
        clock: Callable[[], float] = time.monotonic,
        stats: RuntimeStats | None = None,
    ) -> None:
        self.rest_client = rest_client
        self.stats = stats or RuntimeStats()
        self.session_factory = session_factory
        self.cache_seconds = cache_seconds
        self.clock = clock
        # (symbol, interval) -> (fetched_at, limit used for the request, candles)
        self._cache: dict[tuple[str, str], tuple[float, int, list[KlineUpdate]]] = {}

    async def upsert_candles(self, candles: Sequence[KlineUpdate]) -> None:
        if not candles:
            return
        statement = sqlite_insert(Candle).values([_to_row(candle) for candle in candles])
        statement = statement.on_conflict_do_update(
            index_elements=["symbol", "interval", "open_time"],
            set_={
                "close_time": statement.excluded.close_time,
                **{column: statement.excluded[column] for column in PRICE_COLUMNS},
            },
        )
        try:
            async with self.session_factory() as session:
                await session.execute(statement)
                await session.commit()
        except SQLAlchemyError as exc:
            self.stats.record_db_write_error()
            # One line here; the caller logs the full trace when the error reaches it.
            logger.error(
                "Failed to store %d candle(s) for %s: %r", len(candles), candles[0].symbol, exc
            )
            raise
        self.stats.record_candles_written(len(candles))

    async def handle_event(self, event: MarketEvent) -> None:
        """MarketDataService subscriber: store every closed 1m candle from the live stream."""
        if event["type"] != "kline":
            return
        kline: KlineUpdate = event["data"]
        if kline.interval != STORED_INTERVAL or not kline.is_closed:
            return
        await self.upsert_candles([kline])

    async def get_candles(self, symbol: str, interval: str, limit: int) -> list[KlineUpdate]:
        cached = self._cache.get((symbol, interval))
        if cached is not None:
            fetched_at, cached_limit, candles = cached
            # A page refresh within the TTL must not spend Binance request weight again.
            if self.clock() - fetched_at < self.cache_seconds and cached_limit >= limit:
                return candles[-limit:]

        try:
            candles = await self.rest_client.get_klines(symbol, interval, limit)
        except ExchangeUnavailableError:
            if interval != STORED_INTERVAL:
                raise
            logger.warning("Binance REST unavailable; serving %s 1m candles from SQLite", symbol)
            return await self._load_from_db(symbol, interval, limit)

        candles.sort(key=lambda candle: candle.open_time)
        if interval == STORED_INTERVAL:
            # The newest candle is still open; it gets stored once the live stream closes it.
            await self.upsert_candles([candle for candle in candles if candle.is_closed])
        self._cache[(symbol, interval)] = (self.clock(), limit, candles)
        return candles

    async def _load_from_db(self, symbol: str, interval: str, limit: int) -> list[KlineUpdate]:
        query = (
            select(Candle)
            .where(Candle.symbol == symbol, Candle.interval == interval)
            .order_by(Candle.open_time.desc())
            .limit(limit)
        )
        async with self.session_factory() as session:
            rows = (await session.scalars(query)).all()
        return [_from_row(row) for row in reversed(rows)]
