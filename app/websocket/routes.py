import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from app.api.dependencies import get_connection_manager, get_market_service
from app.api.routes_market import to_market_view
from app.schemas.market import MarketsResponse
from app.services.market_data_service import MarketDataService, MarketEvent
from app.websocket.manager import ConnectionManager

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_BROWSER_MESSAGE_BYTES = 1024

MarketService = Annotated[MarketDataService, Depends(get_market_service)]
Manager = Annotated[ConnectionManager, Depends(get_connection_manager)]


def snapshot_message(service: MarketDataService) -> dict[str, Any]:
    snapshot = service.snapshot()
    response = MarketsResponse(
        connection_status=snapshot["connection_status"],
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


@router.websocket("/ws/market")
async def market_websocket(websocket: WebSocket, service: MarketService, manager: Manager) -> None:
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
            # Browsers only listen; nothing they send has a meaning yet, so it is ignored.
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket)


def browser_message_size(message: dict[str, Any]) -> int:
    if message.get("bytes") is not None:
        return len(message["bytes"])
    return len((message.get("text") or "").encode())
