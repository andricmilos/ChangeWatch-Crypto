from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.api.dependencies import (
    get_alert_service,
    get_connection_manager,
    get_market_service,
    get_runtime_stats,
)
from app.services.alert_service import AlertService
from app.services.market_data_service import MarketDataService
from app.services.runtime_stats import RuntimeStats
from app.websocket.manager import ConnectionManager

router = APIRouter(prefix="/api/status", tags=["status"])

Stats = Annotated[RuntimeStats, Depends(get_runtime_stats)]
MarketService = Annotated[MarketDataService, Depends(get_market_service)]
Manager = Annotated[ConnectionManager, Depends(get_connection_manager)]
Alerts = Annotated[AlertService, Depends(get_alert_service)]


@router.get("")
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
    return status
