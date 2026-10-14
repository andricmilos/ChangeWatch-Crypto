"""Fan-out of market data to every connected browser over /ws/market.

Messages sent to the browser (prices are JSON strings, e.g. "113250.55"):

    {"type": "snapshot", "data": {"connection_status": "connected", "markets": [ ... ]}}
    {"type": "ticker",   "data": { ...MarketView... }}
    {"type": "kline",    "data": { ...KlineUpdate... }}
    {"type": "status",   "data": {"connection_status": "reconnecting"}}

The first message after connecting is always "snapshot".
"""

import asyncio
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)

SEND_TIMEOUT_SECONDS = 5


class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        # discard: broadcast may already have removed a broken connection.
        self.active_connections.discard(websocket)

    async def broadcast(self, message: dict[str, Any]) -> None:
        # Send to all browsers concurrently so one slow browser does not delay the others.
        await asyncio.gather(
            *(self._send(websocket, message) for websocket in list(self.active_connections))
        )

    async def _send(self, websocket: WebSocket, message: dict[str, Any]) -> None:
        try:
            await asyncio.wait_for(websocket.send_json(message), timeout=SEND_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info("Dropping browser WebSocket after failed send: %r", exc)
            self.disconnect(websocket)
