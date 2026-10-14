from typing import Annotated, Any

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from app.api.dependencies import get_connection_manager, get_market_service
from app.api.routes_market import to_market_view
from app.schemas.market import MarketsResponse
from app.services.market_data_service import MarketDataService, MarketEvent
from app.websocket.manager import ConnectionManager

router = APIRouter()

MarketService = Annotated[MarketDataService, Depends(get_market_service)]
Manager = Annotated[ConnectionManager, Depends(get_connection_manager)]


def snapshot_message(service: MarketDataService) -> dict[str, Any]:
    snapshot = service.snapshot()
    response = MarketsResponse(
        connection_status=snapshot["connection_status"],
        markets=[to_market_view(market) for market in snapshot["markets"]],
    )
    return {"type": "snapshot", "data": response.model_dump(mode="json")}


def market_event_message(event: MarketEvent, service: MarketDataService) -> dict[str, Any]:
    """Turn a MarketDataService event into the JSON message browsers receive."""
    if event["type"] == "ticker":
        # The browser gets the same MarketView as the REST API, including freshness.
        symbol = event["data"].symbol
        market = next(m for m in service.snapshot()["markets"] if m["symbol"] == symbol)
        return {"type": "ticker", "data": to_market_view(market).model_dump(mode="json")}
    if event["type"] == "kline":
        return {"type": "kline", "data": event["data"].model_dump(mode="json")}
    return {"type": event["type"], "data": event["data"]}


@router.websocket("/ws/market")
async def market_websocket(websocket: WebSocket, service: MarketService, manager: Manager) -> None:
    await manager.connect(websocket)
    try:
        await websocket.send_json(snapshot_message(service))
        while True:
            # Browsers only listen; anything they send is ignored.
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
