import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from app.config import Settings
from app.schemas.market import KlineUpdate, MarketTicker, RollingChange
from app.services.exchange_client import (
    BinanceStreamClient,
    ClientStatus,
    build_stream_url,
    to_display_symbol,
)
from app.services.runtime_stats import RuntimeStats

logger = logging.getLogger(__name__)

Freshness = Literal["live", "stale", "no_data"]
MarketEvent = dict[str, Any]
Subscriber = Callable[[MarketEvent], Awaitable[None]]


def _same_market_data(old: MarketTicker, new: MarketTicker) -> bool:
    return (
        old.last_price == new.last_price
        and old.price_change == new.price_change
        and old.price_change_percent == new.price_change_percent
        and old.high_24h == new.high_24h
        and old.low_24h == new.low_24h
        and old.volume_24h == new.volume_24h
    )


class MarketDataService:
    """In-memory source of truth for the latest market data, fed by one Binance connection."""

    def __init__(self, settings: Settings, stats: RuntimeStats | None = None) -> None:
        self.settings = settings
        self.stats = stats or RuntimeStats()
        self.client = BinanceStreamClient(
            build_stream_url(
                settings.binance_ws_url,
                settings.supported_symbols,
                settings.kline_intervals,
                settings.rolling_windows,
            ),
            on_ticker=self.handle_ticker,
            on_kline=self.handle_kline,
            on_rolling=self.handle_rolling,
            allowed_symbols=set(settings.supported_symbols),
            receive_timeout=settings.receive_timeout_seconds,
            reconnect_initial_delay=settings.reconnect_initial_delay,
            reconnect_max_delay=settings.reconnect_max_delay,
            on_status_change=self.handle_status,
            stats=self.stats,
        )
        self._tickers: dict[str, MarketTicker] = {}
        self._klines: dict[tuple[str, str], KlineUpdate] = {}
        # Kept apart from _tickers, so a rolling message can never touch the 24h data.
        self._rolling: dict[tuple[str, str], RollingChange] = {}
        self._last_update_at: dict[str, datetime] = {}
        self._subscribers: list[Subscriber] = []
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self.client.run())

    async def stop(self) -> None:
        if self._task is None:
            return
        await self.client.stop()
        await self._task
        self._task = None

    def subscribe(self, callback: Subscriber) -> None:
        self._subscribers.append(callback)

    async def handle_ticker(self, ticker: MarketTicker) -> None:
        previous = self._tickers.get(ticker.symbol)
        if previous is not None and ticker.event_time < previous.event_time:
            return

        self._last_update_at[ticker.symbol] = datetime.now(UTC)
        if previous is not None and _same_market_data(previous, ticker):
            return

        self._tickers[ticker.symbol] = ticker
        await self._notify({"type": "ticker", "data": ticker})

    async def handle_rolling(self, change: RollingChange) -> None:
        key = (change.symbol, change.window)
        previous = self._rolling.get(key)
        if previous is not None and change.event_time < previous.event_time:
            return

        self._rolling[key] = change
        # Binance sends these every second even when the percentage stays the same.
        if previous is not None and previous.change_percent == change.change_percent:
            return
        await self._notify({"type": "rolling", "data": change})

    async def handle_kline(self, kline: KlineUpdate) -> None:
        key = (kline.symbol, kline.interval)
        previous = self._klines.get(key)
        if previous is not None and kline.open_time < previous.open_time:
            return

        self._klines[key] = kline
        await self._notify({"type": "kline", "data": kline})

    async def handle_status(self, status: ClientStatus) -> None:
        await self._notify({"type": "status", "data": {"connection_status": status}})

    def freshness(self, symbol: str, now: datetime | None = None) -> Freshness:
        if symbol not in self._tickers:
            return "no_data"
        # Binance only sends a ticker when something changes, so a quiet pair (e.g. ADAEUR,
        # minutes between trades) can go long without a message while its price is still
        # current. Freshness therefore follows the shared connection (any stream), not the
        # last message for this one pair.
        last_message_at = self.client.last_message_at
        if self.client.status != "connected" or last_message_at is None:
            return "stale"
        age = (now or datetime.now(UTC)) - last_message_at
        if age.total_seconds() > self.settings.stale_after_seconds:
            return "stale"
        return "live"

    def snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        markets = [
            {
                "symbol": symbol,
                "display_symbol": to_display_symbol(symbol),
                "ticker": self._tickers.get(symbol),
                "change_1h_percent": self._rolling_percent(symbol, "1h"),
                "change_4h_percent": self._rolling_percent(symbol, "4h"),
                "updated_at": self._last_update_at.get(symbol),
                "freshness": self.freshness(symbol, now),
            }
            for symbol in self.settings.supported_symbols
        ]
        return {"connection_status": self.client.status, "markets": markets}

    def _rolling_percent(self, symbol: str, window: str) -> Decimal | None:
        change = self._rolling.get((symbol, window))
        return None if change is None else change.change_percent

    async def _notify(self, event: MarketEvent) -> None:
        for subscriber in list(self._subscribers):
            try:
                await subscriber(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                # One broken consumer (browser broadcast, storage, alerts) must not affect others.
                logger.exception("Market data subscriber failed for %s event", event["type"])
