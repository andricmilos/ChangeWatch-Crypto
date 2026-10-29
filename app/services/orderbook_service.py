import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from app.config import Settings
from app.schemas.orderbook import OrderBookSnapshot, OrderBookView
from app.services.exchange_client import to_display_symbol
from app.services.market_data_service import Freshness

logger = logging.getLogger(__name__)

OrderBookSubscriber = Callable[[OrderBookSnapshot], Awaitable[None]]


class OrderBookService:
    """Latest order book per pair, kept in memory only; fed by the shared Binance connection."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._snapshots: dict[str, OrderBookSnapshot] = {}
        self._subscribers: list[OrderBookSubscriber] = []

    def subscribe(self, callback: OrderBookSubscriber) -> None:
        """Call `callback` with every book whose update id is newer than the pair's last one."""
        self._subscribers.append(callback)

    async def handle_snapshot(self, snapshot: OrderBookSnapshot) -> None:
        previous = self._snapshots.get(snapshot.symbol)
        # Binance's update id only grows, so a smaller one is an older book arriving late.
        if previous is not None and snapshot.last_update_id < previous.last_update_id:
            return
        # An equal id is the same book again: stored so freshness sees it, but not announced.
        is_new = previous is None or snapshot.last_update_id > previous.last_update_id
        self._snapshots[snapshot.symbol] = snapshot
        if is_new:
            await self._notify(snapshot)

    async def _notify(self, snapshot: OrderBookSnapshot) -> None:
        for subscriber in list(self._subscribers):
            try:
                await subscriber(snapshot)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A broken consumer must not stop the book from reaching the others.
                logger.exception("Order book subscriber failed for %s", snapshot.symbol)

    def get(self, symbol: str) -> OrderBookSnapshot | None:
        return self._snapshots.get(symbol)

    def freshness(self, symbol: str, now: datetime | None = None) -> Freshness:
        snapshot = self._snapshots.get(symbol)
        if snapshot is None:
            return "no_data"
        # Unlike tickers, depth arrives every second even without trades, so its own age
        # is a reliable sign of whether the book is current.
        age = (now or datetime.now(UTC)) - snapshot.received_at
        if age.total_seconds() > self.settings.orderbook_stale_seconds:
            return "stale"
        return "live"

    def view(self, symbol: str, now: datetime | None = None) -> OrderBookView:
        base = {
            "symbol": symbol,
            "display_symbol": to_display_symbol(symbol),
            "freshness": self.freshness(symbol, now),
        }
        snapshot = self._snapshots.get(symbol)
        if snapshot is None:
            # No data yet: empty lists and null numbers, never 0.
            return OrderBookView(**base)
        return OrderBookView(**{**snapshot.model_dump(), **base})
