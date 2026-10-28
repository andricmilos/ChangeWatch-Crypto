from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_connection_manager, get_market_service
from app.config import Settings
from app.main import app
from app.services.exchange_client import parse_ticker
from app.services.market_data_service import MarketDataService
from app.websocket.manager import ConnectionManager
from app.websocket.routes import market_event_message

BTC_TICKER = {
    "e": "24hrTicker",
    "E": 1760380800000,
    "s": "BTCEUR",
    "p": "-440.10",
    "P": "-0.60",
    "c": "72416.85",
    "h": "73350.00",
    "l": "71820.10",
    "v": "412.551",
    "b": "99.75",
    "a": "100.25",
}


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
async def fake_service():
    # Never started, so there is no Binance connection; data is written in directly.
    service = MarketDataService(Settings(_env_file=None))
    service.client.status = "connected"
    # Freshness follows the connection: it counts as live while any stream sends messages.
    service.client.last_message_at = datetime.now(UTC)
    await service.handle_ticker(parse_ticker(BTC_TICKER))
    return service


@pytest.fixture
def client(fake_service, lifespan_starts):
    original_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_market_service] = lambda: fake_service
    app.dependency_overrides[get_connection_manager] = lambda: ConnectionManager()
    # No `with` block: the lifespan (and the real Binance client) does not start.
    yield TestClient(app)
    app.dependency_overrides.clear()
    app.dependency_overrides.update(original_overrides)
    assert lifespan_starts == []


class FakeWebSocket:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    async def accept(self):
        return None

    async def send_json(self, message):
        if self.fail:
            raise RuntimeError("browser went away")
        self.sent.append(message)


def test_first_message_is_snapshot(client):
    with client.websocket_connect("/ws/market") as websocket:
        message = websocket.receive_json()

    assert message["type"] == "snapshot"
    assert message["data"]["connection_status"] == "connected"
    markets = message["data"]["markets"]
    assert [m["symbol"] for m in markets] == ["BTCEUR", "ETHEUR", "SOLEUR", "BNBEUR", "ADAEUR"]
    assert markets[0]["last_price"] == "72416.85"
    assert markets[0]["freshness"] == "live"
    assert markets[0]["spread_percent"] == "0.500"
    assert markets[1]["spread_percent"] is None
    assert markets[1]["last_price"] is None
    assert markets[1]["freshness"] == "no_data"


def test_messages_from_browser_are_ignored(client):
    with client.websocket_connect("/ws/market") as websocket:
        websocket.receive_json()
        websocket.send_text("hello")
        websocket.send_text("still here")


async def test_broadcast_reaches_all_connections():
    manager = ConnectionManager()
    first, second = FakeWebSocket(), FakeWebSocket()
    await manager.connect(first)
    await manager.connect(second)

    message = {"type": "status", "data": {"connection_status": "reconnecting"}}
    await manager.broadcast(message)

    assert first.sent == [message]
    assert second.sent == [message]


async def test_failing_connection_is_removed_and_others_still_receive():
    manager = ConnectionManager()
    healthy, broken = FakeWebSocket(), FakeWebSocket(fail=True)
    await manager.connect(healthy)
    await manager.connect(broken)

    await manager.broadcast({"type": "status", "data": {"connection_status": "connected"}})
    await manager.broadcast({"type": "status", "data": {"connection_status": "reconnecting"}})

    assert broken not in manager.active_connections
    assert healthy in manager.active_connections
    assert [m["data"]["connection_status"] for m in healthy.sent] == ["connected", "reconnecting"]


def test_disconnect_twice_is_safe():
    manager = ConnectionManager()
    websocket = FakeWebSocket()
    manager.active_connections.add(websocket)

    manager.disconnect(websocket)
    manager.disconnect(websocket)

    assert manager.active_connections == set()


async def test_ticker_event_becomes_market_view_with_string_prices(fake_service):
    event = {"type": "ticker", "data": parse_ticker(BTC_TICKER)}

    message = market_event_message(event, fake_service)

    assert message["type"] == "ticker"
    assert message["data"]["symbol"] == "BTCEUR"
    assert message["data"]["last_price"] == "72416.85"
    assert message["data"]["freshness"] == "live"
    assert message["data"]["bid_price"] == "99.75"
    assert message["data"]["ask_price"] == "100.25"
    assert message["data"]["spread"] == "0.50"
    assert message["data"]["spread_percent"] == "0.500"
    assert message["data"]["high_spread"] is False


async def test_client_status_change_reaches_subscribers():
    service = MarketDataService(Settings(_env_file=None))
    events = []

    async def record(event):
        events.append(event)

    service.subscribe(record)
    await service.client._set_status("reconnecting")
    await service.client._set_status("reconnecting")

    assert events == [
        {
            "type": "status",
            "data": {"connection_status": "reconnecting", "price_source": "ws"},
        }
    ]
    assert market_event_message(events[0], service) == events[0]
