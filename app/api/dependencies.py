from collections.abc import Collection
from typing import TYPE_CHECKING

from fastapi import HTTPException
from fastapi.requests import HTTPConnection

from app.services.candle_service import CandleService
from app.services.history_service import HistoryService
from app.services.market_data_service import MarketDataService
from app.services.runtime_stats import RuntimeStats
from app.websocket.manager import ConnectionManager

if TYPE_CHECKING:
    # Only for the type hint: alert schemas import normalize_symbol from this module.
    from app.services.alert_service import AlertService


# HTTPConnection (not Request) so the same dependencies work in HTTP and WebSocket routes.
def get_market_service(connection: HTTPConnection) -> MarketDataService:
    # A dependency (not a global) so tests can swap in a fake via app.dependency_overrides.
    return connection.app.state.market_service


def get_candle_service(connection: HTTPConnection) -> CandleService:
    return connection.app.state.candle_service


def get_history_service(connection: HTTPConnection) -> HistoryService:
    return connection.app.state.history_service


def get_connection_manager(connection: HTTPConnection) -> ConnectionManager:
    return connection.app.state.connection_manager


def get_runtime_stats(connection: HTTPConnection) -> RuntimeStats:
    return connection.app.state.runtime_stats


def get_alert_service(connection: HTTPConnection) -> "AlertService":
    return connection.app.state.alert_service


def normalize_symbol(raw: str, allowed: Collection[str]) -> str:
    """Turn "btceur", "BTC-EUR" or "BTC/EUR" into "BTCEUR"; unknown pairs are a 404."""
    symbol = raw.strip().upper().replace("-", "").replace("/", "")
    if symbol not in allowed:
        raise HTTPException(status_code=404, detail=f"Unknown symbol: {raw}")
    return symbol
