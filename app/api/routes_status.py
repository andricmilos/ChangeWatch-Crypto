from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.api.dependencies import (
    get_alert_service,
    get_connection_manager,
    get_market_service,
    get_runtime_stats,
)
from app.schemas.status import StatusResponse
from app.services.alert_service import AlertService
from app.services.market_data_service import MarketDataService
from app.services.runtime_stats import RuntimeStats
from app.websocket.manager import ConnectionManager

router = APIRouter(prefix="/api/status", tags=["status"])

Stats = Annotated[RuntimeStats, Depends(get_runtime_stats)]
MarketService = Annotated[MarketDataService, Depends(get_market_service)]
Manager = Annotated[ConnectionManager, Depends(get_connection_manager)]
Alerts = Annotated[AlertService, Depends(get_alert_service)]


# The model documents the shape only (responses, not response_model): serializing through
# it would change the datetime format from "+00:00" to "Z" for existing clients.
@router.get(
    "",
    summary="Runtime diagnostics",
    description=(
        "Counters and states from memory: Binance connection, message rates, per-pair "
        "freshness, connected browsers, database writes, alerts and event queues."
    ),
    responses={200: {"model": StatusResponse}},
)
async def get_status(
    stats: Stats, service: MarketService, manager: Manager, alert_service: Alerts
) -> dict[str, Any]:
    # Only counters and states from memory: no database query, no paths, no settings values.
    status = stats.snapshot()
    status["markets"] = [
        {
            "symbol": symbol,
            "seconds_since_last_message": stats.seconds_since_pair_message(symbol),
            "freshness": service.freshness(symbol),
        }
        for symbol in service.settings.supported_symbols
    ]
    status["browsers"]["connected"] = len(manager.active_connections)
    status["alerts"]["active"] = alert_service.active_count()
    status["event_queues"] = service.events.snapshot()
    return status
