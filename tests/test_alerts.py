import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.dependencies import get_alert_service, get_market_service
from app.config import Settings
from app.database import Base
from app.main import app
from app.models import PriceAlert
from app.schemas.alert import AlertCreate, AlertUpdate
from app.schemas.market import MarketTicker
from app.services.alert_service import AlertService, is_condition_met
from app.services.exchange_client import to_display_symbol
from app.services.market_data_service import MarketDataService

START = datetime(2025, 10, 13, 18, 40, tzinfo=UTC)


class RecordingManager:
    """Stands in for ConnectionManager and remembers every message sent to browsers."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def broadcast(self, message: dict[str, Any]) -> None:
        self.messages.append(message)


def make_ticker(price: str, symbol: str = "BTCEUR", seconds: int = 0) -> MarketTicker:
    return MarketTicker(
        symbol=symbol,
        display_symbol=to_display_symbol(symbol),
        last_price=Decimal(price),
        event_time=START + timedelta(seconds=seconds),
    )


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
    # Never started, so there is no Binance connection; tickers are fed in directly.
    service = MarketDataService(Settings(_env_file=None))
    service.client.status = "connected"
    # Freshness follows the connection: it counts as live while any stream sends messages.
    service.client.last_message_at = datetime.now(UTC)
    return service


@pytest.fixture
def manager() -> RecordingManager:
    return RecordingManager()


@pytest.fixture
async def alert_service(session_factory, market_service, manager) -> AlertService:
    service = AlertService(session_factory, market_service, manager)
    market_service.subscribe(service.handle_event)
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
def client(alert_service, market_service, lifespan_starts):
    original_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_market_service] = lambda: market_service
    app.dependency_overrides[get_alert_service] = lambda: alert_service
    # No `with` block: the lifespan (real Binance client, real database) does not start.
    yield TestClient(app)
    app.dependency_overrides.clear()
    app.dependency_overrides.update(original_overrides)
    assert lifespan_starts == []


async def create_alert(service: AlertService, target: str, condition: str) -> PriceAlert:
    return await service.create(
        AlertCreate(symbol="BTCEUR", target_price=Decimal(target), condition=condition)
    )


async def load_alert(session_factory, alert_id: int) -> PriceAlert:
    async with session_factory() as session:
        return await session.scalar(select(PriceAlert).where(PriceAlert.id == alert_id))


def triggered_messages(manager: RecordingManager) -> list[dict[str, Any]]:
    return [m for m in manager.messages if m["type"] == "alert_triggered"]


# ---------- Condition ----------


@pytest.mark.parametrize(
    ("condition", "price", "expected"),
    [
        ("above", "75000", True),
        ("above", "74999.99", False),
        ("above", "75000.01", True),
        ("below", "75000", True),
        ("below", "75000.01", False),
        ("below", "74999.99", True),
    ],
)
def test_condition_at_the_boundary(condition, price, expected):
    assert is_condition_met(condition, Decimal(price), Decimal("75000")) is expected


async def test_above_fires_exactly_at_target(alert_service, market_service, manager):
    alert = await create_alert(alert_service, "75000", "above")

    await market_service.handle_ticker(make_ticker("74999.99"))
    assert triggered_messages(manager) == []

    await market_service.handle_ticker(make_ticker("75000", seconds=1))
    [message] = triggered_messages(manager)
    assert message["data"]["id"] == alert.id
    assert message["data"]["is_active"] is False
    assert message["data"]["triggered_price"] == "75000"
    assert message["data"]["display_symbol"] == "BTC/EUR"


async def test_below_fires_exactly_at_target(alert_service, market_service, manager):
    await create_alert(alert_service, "74999.99", "below")

    await market_service.handle_ticker(make_ticker("75000"))
    assert triggered_messages(manager) == []

    await market_service.handle_ticker(make_ticker("74999.99", seconds=1))
    assert len(triggered_messages(manager)) == 1


async def test_alert_for_another_symbol_is_not_checked(alert_service, market_service, manager):
    await create_alert(alert_service, "1", "above")

    await market_service.handle_ticker(make_ticker("2100", symbol="ETHEUR"))

    assert triggered_messages(manager) == []


# ---------- Exactly once ----------


async def test_100_rapid_tickers_fire_exactly_once(
    alert_service, market_service, manager, session_factory
):
    alert = await create_alert(alert_service, "75000", "above")
    await market_service.handle_ticker(make_ticker("75001"))

    # Every event above the target arrives at once, as if messages raced each other.
    events = [
        {"type": "ticker", "data": make_ticker(f"{75001 + i}", seconds=i)} for i in range(100)
    ]
    await asyncio.gather(*(alert_service.handle_event(event) for event in events))

    assert len(triggered_messages(manager)) == 1
    saved = await load_alert(session_factory, alert.id)
    assert saved.is_active is False
    assert saved.triggered_price == Decimal("75001")
    assert alert_service.active_alerts("BTCEUR") == []


async def test_alert_already_fired_in_database_is_not_fired_again(
    alert_service, market_service, manager
):
    alert = await create_alert(alert_service, "75000", "above")
    await market_service.handle_ticker(make_ticker("75001"))
    assert len(triggered_messages(manager)) == 1

    # Simulate stale memory (e.g. another trigger in flight): the atomic UPDATE hits 0 rows.
    alert_service._active["BTCEUR"] = [alert]
    await market_service.handle_ticker(make_ticker("75002", seconds=1))

    assert len(triggered_messages(manager)) == 1


async def test_fired_alert_stays_off_after_restart(
    alert_service, market_service, manager, session_factory
):
    await create_alert(alert_service, "75000", "above")
    await market_service.handle_ticker(make_ticker("75001"))

    restarted = AlertService(session_factory, market_service, manager)
    await restarted.load_active()

    assert restarted.active_alerts("BTCEUR") == []


async def test_created_alert_with_condition_already_met_fires_on_next_tick(
    alert_service, market_service, manager
):
    await market_service.handle_ticker(make_ticker("75001"))
    await create_alert(alert_service, "75000", "above")
    assert triggered_messages(manager) == []

    await market_service.handle_ticker(make_ticker("75002", seconds=1))

    assert len(triggered_messages(manager)) == 1


# ---------- Never fires ----------


async def test_disabled_alert_never_fires(alert_service, market_service, manager):
    alert = await create_alert(alert_service, "75000", "above")
    await alert_service.update(alert.id, AlertUpdate(is_active=False))

    for i in range(5):
        await market_service.handle_ticker(make_ticker(f"{75001 + i}", seconds=i))

    assert triggered_messages(manager) == []


async def test_ticker_while_exchange_disconnected_does_not_fire(
    alert_service, market_service, manager
):
    await create_alert(alert_service, "75000", "above")
    market_service.client.status = "reconnecting"

    await market_service.handle_ticker(make_ticker("75001"))

    assert triggered_messages(manager) == []
    assert alert_service.active_alerts("BTCEUR") != []


async def test_stale_ticker_does_not_fire(alert_service, market_service, manager):
    ticker = make_ticker("75001")
    await market_service.handle_ticker(ticker)
    await create_alert(alert_service, "75000", "above")
    # The exchange connection has been silent for longer than STALE_AFTER_SECONDS.
    market_service.client.last_message_at -= timedelta(seconds=60)

    await alert_service.handle_event({"type": "ticker", "data": ticker})

    assert triggered_messages(manager) == []
    assert alert_service.active_alerts("BTCEUR") != []


async def test_ticker_without_market_data_does_not_fire(alert_service, manager):
    await create_alert(alert_service, "75000", "above")

    # The event never went through MarketDataService, so its freshness is "no_data".
    await alert_service.handle_event({"type": "ticker", "data": make_ticker("75001")})

    assert triggered_messages(manager) == []


# ---------- REST API ----------


def test_create_alert(client):
    response = client.post(
        "/api/alerts",
        json={"symbol": "btc-eur", "target_price": "75000.5", "condition": "above"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["symbol"] == "BTCEUR"
    assert body["target_price"] == "75000.5"
    assert body["condition"] == "above"
    assert body["is_active"] is True
    assert body["triggered_at"] is None
    assert body["triggered_price"] is None


def test_created_alert_is_loaded_into_memory(client, alert_service):
    client.post(
        "/api/alerts", json={"symbol": "ETHEUR", "target_price": "2000", "condition": "below"}
    )

    [alert] = alert_service.active_alerts("ETHEUR")
    assert alert.target_price == Decimal("2000")


@pytest.mark.parametrize(
    "payload",
    [
        {"symbol": "BTCEUR", "target_price": "-1", "condition": "above"},
        {"symbol": "BTCEUR", "target_price": "0", "condition": "above"},
        {"symbol": "BTCEUR", "target_price": "1.123456789", "condition": "above"},
        {"symbol": "BTCEUR", "target_price": "NaN", "condition": "above"},
        {"symbol": "BTCEUR", "target_price": "75000", "condition": "sideways"},
        {"symbol": "DOGEEUR", "target_price": "75000", "condition": "above"},
        {"symbol": "BTCEUR", "condition": "above"},
    ],
)
def test_invalid_alert_returns_422(client, payload):
    assert client.post("/api/alerts", json=payload).status_code == 422
    assert client.get("/api/alerts").json() == []


def test_list_alerts_filters_active(client):
    first = client.post(
        "/api/alerts", json={"symbol": "BTCEUR", "target_price": "1", "condition": "below"}
    ).json()
    second = client.post(
        "/api/alerts", json={"symbol": "SOLEUR", "target_price": "100", "condition": "above"}
    ).json()
    client.patch(f"/api/alerts/{first['id']}", json={"is_active": False})

    assert [a["id"] for a in client.get("/api/alerts").json()] == [second["id"], first["id"]]
    assert [a["id"] for a in client.get("/api/alerts?active=true").json()] == [second["id"]]


async def test_patch_reenables_triggered_alert(client, alert_service, market_service, manager):
    created = client.post(
        "/api/alerts", json={"symbol": "BTCEUR", "target_price": "75000", "condition": "above"}
    ).json()
    await market_service.handle_ticker(make_ticker("75001"))
    triggered = client.get("/api/alerts").json()[0]
    assert triggered["is_active"] is False
    assert triggered["triggered_at"] is not None

    response = client.patch(f"/api/alerts/{created['id']}", json={"is_active": True})

    assert response.status_code == 200
    body = response.json()
    assert body["is_active"] is True
    assert body["triggered_at"] is None
    assert body["triggered_price"] is None
    # Back in memory, so it can fire once more.
    await market_service.handle_ticker(make_ticker("75002", seconds=1))
    assert len(triggered_messages(manager)) == 2


def test_patch_changes_target_price(client, alert_service):
    created = client.post(
        "/api/alerts", json={"symbol": "BTCEUR", "target_price": "75000", "condition": "above"}
    ).json()

    response = client.patch(f"/api/alerts/{created['id']}", json={"target_price": "80000.25"})

    assert response.status_code == 200
    assert response.json()["target_price"] == "80000.25"
    assert alert_service.active_alerts("BTCEUR")[0].target_price == Decimal("80000.25")


def test_patch_rejects_invalid_target_price(client):
    created = client.post(
        "/api/alerts", json={"symbol": "BTCEUR", "target_price": "75000", "condition": "above"}
    ).json()

    response = client.patch(f"/api/alerts/{created['id']}", json={"target_price": "-5"})

    assert response.status_code == 422


def test_delete_alert(client, alert_service):
    created = client.post(
        "/api/alerts", json={"symbol": "BTCEUR", "target_price": "75000", "condition": "above"}
    ).json()

    response = client.delete(f"/api/alerts/{created['id']}")

    assert response.status_code == 204
    assert response.content == b""
    assert client.get("/api/alerts").json() == []
    assert alert_service.active_alerts("BTCEUR") == []


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [("patch", {"json": {"is_active": True}}), ("delete", {})],
)
def test_missing_alert_returns_404(client, method, kwargs):
    assert getattr(client, method)("/api/alerts/999", **kwargs).status_code == 404


# ---------- HTMX partials ----------


def test_alerts_partial_lists_sections(client):
    html = client.get("/partials/alerts").text

    assert "Aktivni" in html
    assert "Okinuti" in html
    assert "Nema aktivnih alerta." in html


def test_create_alert_through_partial(client, alert_service):
    response = client.post(
        "/partials/alerts",
        data={"symbol": "BTCEUR", "condition": "above", "target_price": "75000"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "BTC/EUR" in response.text
    assert "iznad" in response.text
    assert "75000 EUR" in response.text
    assert len(alert_service.active_alerts("BTCEUR")) == 1


def test_invalid_partial_form_shows_error_and_creates_nothing(client):
    response = client.post(
        "/partials/alerts",
        data={"symbol": "DOGEEUR", "condition": "above", "target_price": "-1"},
    )

    assert "Neispravan alert" in response.text
    assert client.get("/api/alerts").json() == []
