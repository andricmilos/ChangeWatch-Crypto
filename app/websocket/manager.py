"""Fan-out of market data to every connected browser over /ws/market.

Messages sent to the browser (prices are JSON strings, e.g. "72416.85"):

    {"type": "snapshot", "data": {"connection_status": "connected", "markets": [ ... ]}}
    {"type": "ticker",   "data": { ...MarketView... }}
    {"type": "kline",    "data": { ...KlineUpdate... }}
    {"type": "status",   "data": {"connection_status": "reconnecting"}}
    {"type": "alert_triggered", "data": { ...AlertOut... }}
    {"type": "orderbook", "data": { ...OrderBookSnapshot... }}   (subscribed pair only)
    {"type": "error",    "data": {"detail": "Unknown symbol: XRPEUR"}}

The first message after connecting is always "snapshot".

Messages from the browser (JSON text up to 1 KB; anything else, or an unknown action, is
ignored):

    {"action": "subscribe_orderbook", "symbol": "BTCEUR"}
    {"action": "unsubscribe_orderbook"}
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from fastapi import WebSocket

from app.services.runtime_stats import RuntimeStats

logger = logging.getLogger(__name__)

SEND_TIMEOUT_SECONDS = 5
MAX_CONNECTIONS = 100
# 1013 "Try Again Later": the browser's reconnect loop simply tries again later.
TRY_AGAIN_LATER = 1013


@dataclass
class OrderBookSubscription:
    symbol: str
    # Update id of the last book sent on this subscription, so the same book never goes twice.
    last_sent_update_id: int | None = None


class ConnectionManager:
    def __init__(self, stats: RuntimeStats | None = None) -> None:
        self.active_connections: set[WebSocket] = set()
        # At most one per connection: the browser shows one pair's book at a time.
        self.orderbook_subscriptions: dict[WebSocket, OrderBookSubscription] = {}
        self.stats = stats or RuntimeStats()

    async def connect(self, websocket: WebSocket) -> bool:
        """Accept the browser, or close it with 1013 when the server is full."""
        await websocket.accept()
        if len(self.active_connections) >= MAX_CONNECTIONS:
            # Accept first: a close before accept reaches the browser as a failed handshake
            # without the 1013 code.
            logger.warning("Refusing browser WebSocket: %d connections open", MAX_CONNECTIONS)
            await websocket.close(code=TRY_AGAIN_LATER)
            return False
        self.active_connections.add(websocket)
        return True

    def disconnect(self, websocket: WebSocket) -> None:
        # discard: broadcast may already have removed a broken connection.
        self.active_connections.discard(websocket)
        self.orderbook_subscriptions.pop(websocket, None)

    async def broadcast(self, message: dict[str, Any]) -> None:
        # Send to all browsers concurrently so one slow browser does not delay the others.
        await asyncio.gather(
            *(self.send(websocket, message) for websocket in list(self.active_connections))
        )

    def subscribe_orderbook(self, websocket: WebSocket, symbol: str) -> None:
        """Replace the connection's order book subscription, if any, with `symbol`."""
        self.orderbook_subscriptions[websocket] = OrderBookSubscription(symbol)

    def unsubscribe_orderbook(self, websocket: WebSocket) -> None:
        self.orderbook_subscriptions.pop(websocket, None)

    async def send_orderbook(
        self,
        symbol: str,
        update_id: int,
        message: dict[str, Any],
        only: WebSocket | None = None,
    ) -> None:
        """Send a book to the connections subscribed to `symbol` that have not had it yet.

        `only` limits the send to one connection, for the book sent right after subscribing.
        """
        targets = []
        for websocket, subscription in list(self.orderbook_subscriptions.items()):
            if only is not None and websocket is not only:
                continue
            if subscription.symbol != symbol:
                continue
            last_sent = subscription.last_sent_update_id
            if last_sent is not None and update_id <= last_sent:
                continue
            # Marked before the await, so the same book arriving concurrently is skipped.
            subscription.last_sent_update_id = update_id
            targets.append(websocket)
        await asyncio.gather(*(self.send(websocket, message) for websocket in targets))

    async def send(self, websocket: WebSocket, message: dict[str, Any]) -> None:
        try:
            await asyncio.wait_for(websocket.send_json(message), timeout=SEND_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.stats.record_browser_send(ok=False)
            logger.info("Dropping browser WebSocket after failed send: %r", exc)
            self.disconnect(websocket)
            return
        self.stats.record_browser_send(ok=True)
