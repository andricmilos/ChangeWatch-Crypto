import json
import logging
from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from app.api.dependencies import (
    get_connection_manager,
    get_market_service,
    get_orderbook_service,
    parse_symbol,
)
from app.api.routes_market import to_market_view
from app.schemas.market import MarketsResponse
from app.schemas.orderbook import OrderBookSnapshot
from app.services.market_data_service import MarketDataService, MarketEvent
from app.services.orderbook_service import OrderBookService
from app.websocket.manager import ConnectionManager

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_BROWSER_MESSAGE_BYTES = 1024

MarketService = Annotated[MarketDataService, Depends(get_market_service)]
Manager = Annotated[ConnectionManager, Depends(get_connection_manager)]
OrderBooks = Annotated[OrderBookService, Depends(get_orderbook_service)]


def snapshot_message(service: MarketDataService) -> dict[str, Any]:
    snapshot = service.snapshot()
    response = MarketsResponse(
        connection_status=snapshot["connection_status"],
        price_source=snapshot["price_source"],
        markets=[
            to_market_view(market, service.settings.spread_warning_percent)
            for market in snapshot["markets"]
        ],
    )
    return {"type": "snapshot", "data": response.model_dump(mode="json")}


def market_event_message(event: MarketEvent, service: MarketDataService) -> dict[str, Any]:
    """Turn a MarketDataService event into the JSON message browsers receive."""
    if event["type"] in ("ticker", "rolling"):
        # The browser gets the same MarketView as the REST API, including freshness; a
        # rolling change is just another "ticker" message to the browser.
        symbol = event["data"].symbol
        market = next(m for m in service.snapshot()["markets"] if m["symbol"] == symbol)
        view = to_market_view(market, service.settings.spread_warning_percent)
        return {"type": "ticker", "data": view.model_dump(mode="json")}
    if event["type"] == "kline":
        return {"type": "kline", "data": event["data"].model_dump(mode="json")}
    return {"type": event["type"], "data": event["data"]}


def orderbook_message(snapshot: OrderBookSnapshot) -> dict[str, Any]:
    # mode="json" turns every Decimal into a string, so no precision is lost.
    return {"type": "orderbook", "data": snapshot.model_dump(mode="json")}


def error_message(detail: str) -> dict[str, Any]:
    return {"type": "error", "data": {"detail": detail}}


@router.websocket("/ws/market")
async def market_websocket(
    websocket: WebSocket, service: MarketService, manager: Manager, orderbooks: OrderBooks
) -> None:
    if not await manager.connect(websocket):
        return
    try:
        await websocket.send_json(snapshot_message(service))
        manager.stats.record_browser_send(ok=True)
        while True:
            # receive() instead of receive_text(): a binary frame must not crash the loop.
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if browser_message_size(message) > MAX_BROWSER_MESSAGE_BYTES:
                logger.debug("Ignoring browser message over %d bytes", MAX_BROWSER_MESSAGE_BYTES)
                continue
            await handle_browser_message(websocket, message, manager, orderbooks)
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket)


async def handle_browser_message(
    websocket: WebSocket,
    message: Mapping[str, Any],
    manager: ConnectionManager,
    orderbooks: OrderBookService,
) -> None:
    text = message.get("text")
    if text is None:
        return
    try:
        payload = json.loads(text)
    except ValueError:
        logger.debug("Ignoring browser message that is not JSON")
        return
    if not isinstance(payload, dict):
        return
    action = payload.get("action")
    if action == "subscribe_orderbook":
        await subscribe_orderbook(websocket, payload.get("symbol"), manager, orderbooks)
    elif action == "unsubscribe_orderbook":
        manager.unsubscribe_orderbook(websocket)
    # Unknown actions are ignored, so an older server does not break a newer page.


async def subscribe_orderbook(
    websocket: WebSocket, raw_symbol: Any, manager: ConnectionManager, orderbooks: OrderBookService
) -> None:
    allowed = orderbooks.settings.supported_symbols
    symbol = parse_symbol(raw_symbol, allowed) if isinstance(raw_symbol, str) else None
    if symbol is None:
        # The connection and any existing subscription stay as they are.
        await manager.send(websocket, error_message(f"Unknown symbol: {raw_symbol}"))
        return
    manager.subscribe_orderbook(websocket, symbol)
    snapshot = orderbooks.get(symbol)
    if snapshot is not None:
        await manager.send_orderbook(
            symbol, snapshot.last_update_id, orderbook_message(snapshot), only=websocket
        )


def browser_message_size(message: Mapping[str, Any]) -> int:
    if message.get("bytes") is not None:
        return len(message["bytes"])
    return len((message.get("text") or "").encode())
