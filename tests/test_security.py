from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from starlette.websockets import WebSocketDisconnect

from app.api.dependencies import (
    get_alert_service,
    get_candle_service,
    get_connection_manager,
    get_market_service,
)
from app.api.rate_limit import RateLimiter
from app.api.routes_alerts import MAX_ACTIVE_ALERTS
from app.config import Settings
from app.database import Base, get_session
from app.main import app
from app.schemas.alert import AlertCreate
from app.services.alert_service import AlertService
from app.services.exchange_client import ExchangeUnavailableError
from app.services.market_data_service import MarketDataService
from app.websocket.manager import MAX_CONNECTIONS, ConnectionManager

ALERT = {"symbol": "BTCEUR", "target_price": "75000", "condition": "above"}


class FailingCandleService:
    """Stands in for CandleService and raises instead of calling Binance."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def get_candles(self, symbol: str, interval: str, limit: int):
        raise self.error


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    # In-memory SQLite (StaticPool: one shared connection), so tests never touch
    # data/changewatch.db.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def market_service() -> MarketDataService:
    # Never started, so there is no Binance connection.
    return MarketDataService(Settings(_env_file=None))


@pytest.fixture
def manager() -> ConnectionManager:
    return ConnectionManager()


@pytest.fixture
async def alert_service(session_factory, market_service, manager) -> AlertService:
    service = AlertService(session_factory, market_service, manager)
    await service.load_active()
    return service


@pytest.fixture
def lifespan_starts(monkeypatch):
    """Fail loudly if anything starts a real MarketDataService (and so a Binance connection)."""
    starts = []

    async def forbidden_start(self):
        starts.append(self)
        raise AssertionError("Tests must not start the real Binance connection")

    monkeypatch.setattr(MarketDataService, "start", forbidden_start)
    return starts


@pytest.fixture
def overrides(market_service, alert_service, manager, lifespan_starts):
    original_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_market_service] = lambda: market_service
    app.dependency_overrides[get_alert_service] = lambda: alert_service
    app.dependency_overrides[get_connection_manager] = lambda: manager
    yield app.dependency_overrides
    app.dependency_overrides.clear()
    app.dependency_overrides.update(original_overrides)
    assert lifespan_starts == []


@pytest.fixture
def client(overrides):
    # No `with` block: the lifespan (real Binance client, real database) does not start.
    # raise_server_exceptions=False: see the 500 response a browser would get.
    return TestClient(app, raise_server_exceptions=False)


# ---------- Rate limit ----------


def test_31st_post_in_a_minute_gets_429(client):
    for _ in range(30):
        assert client.post("/api/alerts", json=ALERT).status_code == 201

    response = client.post("/api/alerts", json=ALERT)

    assert response.status_code == 429
    assert 1 <= int(response.headers["Retry-After"]) <= 60
    assert len(client.get("/api/alerts").json()) == 30


def test_reads_are_not_rate_limited(client):
    for _ in range(30):
        client.post("/api/alerts", json=ALERT)

    assert client.get("/api/alerts").status_code == 200
    assert client.get("/api/markets").status_code == 200


def test_post_patch_and_delete_share_one_budget(client, overrides):
    def no_database():
        raise AssertionError("The limiter must answer before a session is opened")

    overrides[get_session] = no_database
    created = client.post("/api/alerts", json=ALERT).json()
    for _ in range(28):
        client.patch(f"/api/alerts/{created['id']}", json={"is_active": True})

    assert client.delete(f"/api/alerts/{created['id']}").status_code == 204
    assert client.delete("/api/watchlist/BTCEUR").status_code == 429
    assert client.post("/partials/alerts", data=ALERT).status_code == 429


def make_request(ip: str) -> SimpleNamespace:
    return SimpleNamespace(client=SimpleNamespace(host=ip))


def test_limit_is_per_ip_and_window_slides():
    now = [0.0]
    limiter = RateLimiter(max_requests=2, window_seconds=60, clock=lambda: now[0])
    limiter(make_request("1.1.1.1"))
    now[0] = 30
    limiter(make_request("1.1.1.1"))

    with pytest.raises(HTTPException) as blocked:
        limiter(make_request("1.1.1.1"))
    assert blocked.value.status_code == 429
    assert blocked.value.headers == {"Retry-After": "30"}

    # Another address has its own budget.
    limiter(make_request("2.2.2.2"))
    # Once the first request is a minute old, there is room for one more.
    now[0] = 60
    limiter(make_request("1.1.1.1"))


# ---------- Active alert limit ----------


async def test_51st_active_alert_gets_400(client, alert_service):
    for _ in range(MAX_ACTIVE_ALERTS):
        await alert_service.create(AlertCreate(**ALERT))

    response = client.post("/api/alerts", json=ALERT)

    assert response.status_code == 400
    assert "50 aktivnih alerta" in response.json()["detail"]
    assert alert_service.active_count() == MAX_ACTIVE_ALERTS


async def test_51st_alert_through_htmx_form_shows_error(client, alert_service):
    for _ in range(MAX_ACTIVE_ALERTS):
        await alert_service.create(AlertCreate(**ALERT))

    response = client.post("/partials/alerts", data=ALERT)

    assert "50 aktivnih alerta" in response.text
    assert alert_service.active_count() == MAX_ACTIVE_ALERTS


async def test_disabled_alerts_do_not_count(client, alert_service):
    for _ in range(MAX_ACTIVE_ALERTS):
        await alert_service.create(AlertCreate(**ALERT))
    first = client.get("/api/alerts").json()[-1]
    client.patch(f"/api/alerts/{first['id']}", json={"is_active": False})

    assert client.post("/api/alerts", json=ALERT).status_code == 201


# ---------- Error handlers ----------


def test_exchange_unavailable_on_candles_gives_503_without_trace(client, overrides):
    error = ExchangeUnavailableError("Binance REST request failed: ConnectError('boom')")
    overrides[get_candle_service] = lambda: FailingCandleService(error)

    response = client.get("/api/markets/BTCEUR/candles?interval=5m")

    assert response.status_code == 503
    assert response.json() == {"detail": "Berza trenutno nije dostupna"}
    assert "Traceback" not in response.text
    assert "boom" not in response.text


def test_unexpected_error_gives_500_without_trace(client, overrides, caplog):
    error = RuntimeError("secret detail in /Users/someone/app/services/candle_service.py")
    overrides[get_candle_service] = lambda: FailingCandleService(error)

    response = client.get("/api/markets/BTCEUR/candles")

    assert response.status_code == 500
    assert response.json() == {"detail": "Interna greška"}
    assert "secret detail" not in response.text
    # The full trace is in the log instead.
    assert any(record.exc_info and record.exc_info[1] is error for record in caplog.records)


# ---------- Browser WebSocket ----------


def test_101st_websocket_is_closed_with_1013(client, manager):
    for _ in range(MAX_CONNECTIONS):
        manager.active_connections.add(object())

    with client.websocket_connect("/ws/market") as websocket:
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_json()

    assert closed.value.code == 1013
    assert len(manager.active_connections) == MAX_CONNECTIONS


def test_large_and_binary_browser_messages_are_ignored(client, manager):
    with client.websocket_connect("/ws/market") as websocket:
        assert websocket.receive_json()["type"] == "snapshot"
        websocket.send_text("x" * 5000)
        websocket.send_bytes(b"\x00" * 5000)
        websocket.send_bytes(b"small")
        assert len(manager.active_connections) == 1

    assert manager.active_connections == set()


# ---------- Health ----------


def test_health_reports_exchange_connection(client, market_service):
    market_service.client.status = "connected"
    market_service.client.last_message_at = datetime.now(UTC) - timedelta(seconds=3)

    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["exchange_connection"] == "connected"
    assert 3 <= body["seconds_since_last_message"] < 5


def test_health_while_reconnecting_is_still_200(client, market_service):
    market_service.client.status = "reconnecting"

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "degraded",
        "exchange_connection": "reconnecting",
        "seconds_since_last_message": None,
    }


def test_prices_in_alert_api_are_never_floats(client):
    body = client.post("/api/alerts", json=ALERT).json()

    assert body["target_price"] == "75000"
    assert Decimal(body["target_price"]) == Decimal("75000")
