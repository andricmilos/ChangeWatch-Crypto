import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.dependencies import (
    get_alert_service,
    get_connection_manager,
    get_market_service,
    get_orderbook_service,
    get_runtime_stats,
)
from app.config import Settings
from app.database import Base
from app.main import app
from app.schemas.alert import AlertCreate
from app.services.alert_service import AlertService
from app.services.candle_service import CandleService
from app.services.exchange_client import BinanceStreamClient, parse_kline
from app.services.market_data_service import MarketDataService
from app.services.orderbook_service import OrderBookService
from app.services.runtime_stats import RecentCounter, RuntimeStats
from app.websocket.manager import ConnectionManager
from tests.test_alerts import make_ticker
from tests.test_exchange_client import (
    BlockingConnection,
    FakeConnect,
    FakeConnection,
    ticker_message,
    wait_until,
)
from tests.test_market_data import make_kline


class QueuedConnection(BlockingConnection):
    """Returns the queued messages, then stays open until close() is called."""

    async def recv(self):
        if self.messages:
            return self.messages.pop(0)
        return await super().recv()


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_client(connect_factory, stats):
    async def ignore(_event):
        return None

    return BinanceStreamClient(
        "wss://example.invalid/stream",
        on_ticker=ignore,
        on_kline=ignore,
        connect_factory=connect_factory,
        allowed_symbols={"BTCEUR", "ETHEUR"},
        receive_timeout=5,
        stats=stats,
    )


# ---------- Exchange messages and connection ----------


async def test_invalid_message_increases_rejected_count():
    stats = RuntimeStats()
    connection = QueuedConnection(
        ["not json", ticker_message("BTCEUR", "0"), ticker_message("BTCEUR", "72416.85")]
    )
    client = make_client(lambda _url: FakeConnect(connection), stats)

    task = asyncio.create_task(client.run())
    await wait_until(lambda: stats.messages_total == 3)

    assert stats.messages_rejected == 2
    assert stats.seconds_since_pair_message("BTCEUR") is not None
    assert stats.seconds_since_pair_message("ETHEUR") is None
    await client.stop()
    await asyncio.wait_for(task, timeout=1)


async def test_dropped_connection_counts_reconnect_and_keeps_reason(monkeypatch):
    stats = RuntimeStats()
    connections = []

    def connect_factory(_url):
        # The first connection drops right away, the second one stays open.
        connection = FakeConnection([]) if not connections else BlockingConnection([])
        connections.append(connection)
        return FakeConnect(connection)

    client = make_client(connect_factory, stats)

    async def no_sleep(_delay):
        await asyncio.sleep(0)

    monkeypatch.setattr(client, "_sleep_unless_stopped", no_sleep)

    task = asyncio.create_task(client.run())
    await wait_until(lambda: len(connections) == 2 and stats.exchange_status == "connected")

    assert stats.reconnects == 1
    assert stats.last_disconnect_reason == "ConnectionResetError: connection dropped"
    assert stats.last_disconnect_at is not None
    assert stats.connected_since is not None
    await client.stop()
    await asyncio.wait_for(task, timeout=1)
    assert stats.exchange_status == "stopped"
    assert stats.connected_since is None


async def test_failed_connects_are_counted_while_reconnecting(monkeypatch):
    stats = RuntimeStats()

    def connect_factory(_url):
        raise OSError("network is down")

    client = make_client(connect_factory, stats)

    async def stop_after_three(_delay):
        if stats.reconnects == 3:
            await client.stop()

    monkeypatch.setattr(client, "_sleep_unless_stopped", stop_after_three)

    await asyncio.wait_for(client.run(), timeout=1)

    assert stats.reconnects == 3
    assert stats.last_disconnect_reason == "OSError: network is down"
    assert stats.connected_since is None


# ---------- Last 60 seconds ----------


def test_last_60s_does_not_count_older_messages():
    clock = FakeClock()
    stats = RuntimeStats(clock=clock)

    stats.record_message()
    clock.now += 30
    stats.record_message()
    stats.record_message()
    assert stats.messages_last_60s() == 3

    clock.now += 31  # the first message is now 61 s old
    assert stats.messages_last_60s() == 2
    assert stats.messages_total == 3

    clock.now += 60
    assert stats.messages_last_60s() == 0


def test_recent_counter_reuses_slots_after_a_full_window():
    counter = RecentCounter(window=60)
    counter.add(5)
    counter.add(65)  # same slot as second 5, one window later

    assert counter.total(65) == 1
    assert counter.total(124) == 1
    assert counter.total(125) == 0


# ---------- Database and alerts ----------


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_stored_candles_and_write_errors_are_counted(session_factory):
    stats = RuntimeStats()
    candle_service = CandleService(rest_client=None, session_factory=session_factory, stats=stats)

    candle = parse_kline(make_kline(True))
    await candle_service.upsert_candles([candle])
    assert stats.candles_written == 1

    def broken_session():
        raise OperationalError("INSERT", {}, Exception("database is locked"))

    candle_service.session_factory = broken_session
    with pytest.raises(OperationalError):
        await candle_service.upsert_candles([candle])

    assert stats.candles_written == 1
    assert stats.db_write_errors == 1


async def test_fired_alert_is_counted(alert_service, market_service, stats):
    market_service.subscribe(alert_service.handle_event)
    await alert_service.create(
        AlertCreate(symbol="BTCEUR", target_price="75000", condition="above")
    )
    market_service.client.status = "connected"
    market_service.client.last_message_at = datetime.now(UTC)

    await market_service.handle_ticker(make_ticker("75000"))
    await market_service.handle_ticker(make_ticker("76000", seconds=1))

    assert stats.alerts_triggered == 1
    assert alert_service.active_count() == 0


# ---------- GET /api/status ----------


@pytest.fixture
def stats() -> RuntimeStats:
    return RuntimeStats()


@pytest.fixture
def market_service(stats) -> MarketDataService:
    # Never started, so there is no Binance connection.
    return MarketDataService(Settings(_env_file=None), stats)


@pytest.fixture
def manager(stats) -> ConnectionManager:
    return ConnectionManager(stats)


@pytest.fixture
async def alert_service(session_factory, market_service, manager, stats) -> AlertService:
    service = AlertService(session_factory, market_service, manager, stats)
    await service.load_active()
    return service


@pytest.fixture
def client(stats, market_service, manager, alert_service, monkeypatch):
    async def forbidden_start(self):
        raise AssertionError("Tests must not start the real Binance connection")

    monkeypatch.setattr(MarketDataService, "start", forbidden_start)
    original_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_runtime_stats] = lambda: stats
    app.dependency_overrides[get_market_service] = lambda: market_service
    app.dependency_overrides[get_connection_manager] = lambda: manager
    app.dependency_overrides[get_orderbook_service] = lambda: OrderBookService(
        Settings(_env_file=None)
    )
    app.dependency_overrides[get_alert_service] = lambda: alert_service
    # No `with` block: the lifespan (real Binance client, real database) does not start.
    yield TestClient(app)
    app.dependency_overrides.clear()
    app.dependency_overrides.update(original_overrides)


def test_status_returns_expected_fields(client):
    response = client.get("/api/status")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "started_at",
        "uptime_seconds",
        "exchange",
        "messages",
        "orderbook",
        "markets",
        "browsers",
        "database",
        "alerts",
    }
    assert body["exchange"] == {
        "status": "stopped",
        "connected_since": None,
        "reconnects": 0,
        "server_shutdowns": 0,
        "last_disconnect_at": None,
        "last_disconnect_reason": None,
    }
    assert body["messages"] == {"total": 0, "last_60s": 0, "rejected": 0}
    assert body["orderbook"] == {"received": 0, "rejected": 0}
    assert body["markets"][0] == {
        "symbol": "BTCEUR",
        "seconds_since_last_message": None,
        "freshness": "no_data",
    }
    assert [market["symbol"] for market in body["markets"]] == [
        "BTCEUR",
        "ETHEUR",
        "SOLEUR",
        "BNBEUR",
        "ADAEUR",
    ]
    assert body["browsers"] == {"connected": 0, "messages_sent": 0, "send_failures": 0}
    assert body["database"] == {
        "candles_written": 0,
        "write_errors": 0,
        "last_cleanup_at": None,
        "last_cleanup_result": None,
        "last_cleanup_deleted": None,
    }
    assert body["alerts"] == {"active": 0, "triggered": 0}


def test_status_does_not_reveal_sensitive_data(client, stats):
    stats.record_disconnect("ConnectionResetError: connection dropped")
    stats.record_db_write_error()

    text = client.get("/api/status").text

    settings = Settings(_env_file=None)
    for secret in (
        settings.database_url,
        "changewatch.db",
        "/Users/",
        "app/",
        ".py",
        "Traceback",
        "DATABASE_URL",
        "LOG_LEVEL",
        "APP_ENV",
    ):
        assert secret not in text


def test_browser_connect_and_disconnect_change_connected_count(client):
    with client.websocket_connect("/ws/market") as websocket:
        assert websocket.receive_json()["type"] == "snapshot"
        browsers = client.get("/api/status").json()["browsers"]
        assert browsers["connected"] == 1
        assert browsers["messages_sent"] == 1

    assert client.get("/api/status").json()["browsers"]["connected"] == 0


async def test_failed_browser_send_is_counted(stats, manager):
    class BrokenWebSocket:
        async def accept(self):
            return None

        async def send_json(self, message):
            raise RuntimeError("browser went away")

    await manager.connect(BrokenWebSocket())
    await manager.broadcast({"type": "status", "data": {"connection_status": "connected"}})

    assert stats.browser_send_failures == 1
    assert stats.browser_messages_sent == 0
    assert manager.active_connections == set()


def test_health_stays_small(client):
    assert set(client.get("/health").json()) == {
        "status",
        "exchange_connection",
        "seconds_since_last_message",
    }
