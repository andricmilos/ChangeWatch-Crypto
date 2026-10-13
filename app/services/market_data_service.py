import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal

from app.config import Settings
from app.schemas.market import KlineUpdate, MarketTicker
from app.services.exchange_client import (
    BinanceStreamClient,
    build_stream_url,
    to_display_symbol,
)

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

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = BinanceStreamClient(
            build_stream_url(
                settings.binance_ws_url, settings.supported_symbols, settings.kline_intervals
            ),
            on_ticker=self.handle_ticker,
            on_kline=self.handle_kline,
            allowed_symbols=set(settings.supported_symbols),
            receive_timeout=settings.receive_timeout_seconds,
            reconnect_initial_delay=settings.reconnect_initial_delay,
            reconnect_max_delay=settings.reconnect_max_delay,
        )
        self._tickers: dict[str, MarketTicker] = {}
        self._klines: dict[tuple[str, str], KlineUpdate] = {}
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

    async def handle_kline(self, kline: KlineUpdate) -> None:
        key = (kline.symbol, kline.interval)
        previous = self._klines.get(key)
        if previous is not None and kline.open_time < previous.open_time:
            return

        self._klines[key] = kline
        await self._notify({"type": "kline", "data": kline})

    def freshness(self, symbol: str, now: datetime | None = None) -> Freshness:
        last_update_at = self._last_update_at.get(symbol)
        if symbol not in self._tickers or last_update_at is None:
            return "no_data"
        if self.client.status != "connected":
            return "stale"
        age = (now or datetime.now(UTC)) - last_update_at
        if age.total_seconds() > self.settings.stale_after_seconds:
            return "stale"
        return "live"

    def snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        markets = [
            {
                "symbol": symbol,
                "display_symbol": to_display_symbol(symbol),
                "ticker": self._tickers.get(symbol),
                "updated_at": self._last_update_at.get(symbol),
                "freshness": self.freshness(symbol, now),
            }
            for symbol in self.settings.supported_symbols
        ]
        return {"connection_status": self.client.status, "markets": markets}

    async def _notify(self, event: MarketEvent) -> None:
        for subscriber in list(self._subscribers):
            try:
                await subscriber(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                # One broken consumer (browser broadcast, storage, alerts) must not affect others.
                logger.exception("Market data subscriber failed for %s event", event["type"])
