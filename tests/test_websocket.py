from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import (
    get_connection_manager,
    get_market_service,
    get_orderbook_service,
)
from app.config import Settings
from app.main import app
from app.services.exchange_client import parse_ticker
from app.services.market_data_service import MarketDataService
from app.services.orderbook_service import OrderBookService
from app.websocket.manager import ConnectionManager
from app.websocket.routes import market_event_message, orderbook_message
from tests.test_orderbook import make_snapshot

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
def orderbook_service():
    return OrderBookService(Settings(_env_file=None))


@pytest.fixture
def manager():
    return ConnectionManager()


@pytest.fixture
def client(fake_service, orderbook_service, manager, lifespan_starts):
    original_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_market_service] = lambda: fake_service
    app.dependency_overrides[get_orderbook_service] = lambda: orderbook_service
    app.dependency_overrides[get_connection_manager] = lambda: manager
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


# ---------- Order book subscriptions ----------


async def push(manager, snapshot):
    """What the lifespan does with every new book from OrderBookService."""
    await manager.send_orderbook(
        snapshot.symbol, snapshot.last_update_id, orderbook_message(snapshot)
    )


def subscribe(websocket, symbol):
    websocket.send_json({"action": "subscribe_orderbook", "symbol": symbol})


def wait_for_server(websocket):
    """Messages are handled in order, so the error reply means every earlier one is done."""
    subscribe(websocket, "XRPEUR")
    assert websocket.receive_json()["type"] == "error"


async def test_connections_get_only_books_of_their_own_pair():
    manager = ConnectionManager()
    btc_browser, eth_browser = FakeWebSocket(), FakeWebSocket()
    await manager.connect(btc_browser)
    await manager.connect(eth_browser)
    manager.subscribe_orderbook(btc_browser, "BTCEUR")
    manager.subscribe_orderbook(eth_browser, "ETHEUR")

    await push(manager, make_snapshot(last_update_id=1, symbol="BTCEUR"))
    await push(manager, make_snapshot(last_update_id=2, symbol="ETHEUR"))
    await push(manager, make_snapshot(last_update_id=3, symbol="BTCEUR"))

    assert [(m["data"]["symbol"], m["data"]["last_update_id"]) for m in btc_browser.sent] == [
        ("BTCEUR", 1),
        ("BTCEUR", 3),
    ]
    assert [(m["data"]["symbol"], m["data"]["last_update_id"]) for m in eth_browser.sent] == [
        ("ETHEUR", 2)
    ]


async def test_connection_without_subscription_gets_no_books():
    manager = ConnectionManager()
    listener = FakeWebSocket()
    await manager.connect(listener)

    await push(manager, make_snapshot(symbol="BTCEUR"))

    assert listener.sent == []


async def test_new_subscription_replaces_the_old_one():
    manager = ConnectionManager()
    browser = FakeWebSocket()
    await manager.connect(browser)
    manager.subscribe_orderbook(browser, "BTCEUR")
    manager.subscribe_orderbook(browser, "ETHEUR")

    await push(manager, make_snapshot(last_update_id=1, symbol="BTCEUR"))
    await push(manager, make_snapshot(last_update_id=2, symbol="ETHEUR"))

    assert [m["data"]["symbol"] for m in browser.sent] == ["ETHEUR"]


async def test_same_book_is_not_sent_twice():
    manager = ConnectionManager()
    browser = FakeWebSocket()
    await manager.connect(browser)
    manager.subscribe_orderbook(browser, "BTCEUR")

    await push(manager, make_snapshot(last_update_id=160))
    await push(manager, make_snapshot(last_update_id=160))
    await push(manager, make_snapshot(last_update_id=161))

    assert [m["data"]["last_update_id"] for m in browser.sent] == [160, 161]


async def test_orderbook_service_announces_only_newer_books():
    service = OrderBookService(Settings(_env_file=None))
    announced = []

    async def record(snapshot):
        announced.append(snapshot.last_update_id)

    service.subscribe(record)
    for update_id in (160, 160, 150, 161):
        await service.handle_snapshot(make_snapshot(last_update_id=update_id))

    assert announced == [160, 161]


async def test_failing_orderbook_subscriber_does_not_stop_the_others():
    service = OrderBookService(Settings(_env_file=None))
    announced = []

    async def broken(snapshot):
        raise RuntimeError("boom")

    async def record(snapshot):
        announced.append(snapshot.symbol)

    service.subscribe(broken)
    service.subscribe(record)
    await service.handle_snapshot(make_snapshot())

    assert announced == ["BTCEUR"]


async def test_orderbook_message_sends_numbers_as_strings():
    message = orderbook_message(make_snapshot())

    assert message["type"] == "orderbook"
    data = message["data"]
    assert data["symbol"] == "BTCEUR"
    assert data["last_update_id"] == 160
    assert data["bids"][0] == {"price": "100.00", "quantity": "1.5", "cumulative_quantity": "1.5"}
    assert data["best_ask"] == "100.10"
    assert data["spread"] == "0.10"
    assert isinstance(data["imbalance_percent"], str)


async def test_current_book_is_sent_right_after_subscribing(client, orderbook_service):
    await orderbook_service.handle_snapshot(make_snapshot(last_update_id=160))

    with client.websocket_connect("/ws/market") as websocket:
        websocket.receive_json()
        subscribe(websocket, "btc/eur")
        message = websocket.receive_json()

    assert message == orderbook_message(make_snapshot(last_update_id=160))


def test_subscribing_without_a_book_sends_nothing_yet(client, manager):
    with client.websocket_connect("/ws/market") as websocket:
        websocket.receive_json()
        subscribe(websocket, "ETHEUR")
        wait_for_server(websocket)

        assert [s.symbol for s in manager.orderbook_subscriptions.values()] == ["ETHEUR"]


async def test_book_sent_on_subscribe_is_not_sent_again_by_the_broadcast():
    manager = ConnectionManager()
    browser = FakeWebSocket()
    await manager.connect(browser)
    snapshot = make_snapshot(last_update_id=160)
    manager.subscribe_orderbook(browser, "BTCEUR")

    # The subscribe sends the current book; the same book then also reaches the broadcast.
    message = orderbook_message(snapshot)
    await manager.send_orderbook("BTCEUR", 160, message, only=browser)
    await push(manager, snapshot)

    assert browser.sent == [message]


def test_unsubscribe_removes_subscription(client, manager):
    with client.websocket_connect("/ws/market") as websocket:
        websocket.receive_json()
        subscribe(websocket, "BTCEUR")
        websocket.send_json({"action": "unsubscribe_orderbook"})
        wait_for_server(websocket)

        assert manager.orderbook_subscriptions == {}


def test_disconnect_removes_subscription(client, manager):
    with client.websocket_connect("/ws/market") as websocket:
        websocket.receive_json()
        subscribe(websocket, "BTCEUR")
        wait_for_server(websocket)
        assert len(manager.orderbook_subscriptions) == 1

    assert manager.orderbook_subscriptions == {}
    assert manager.active_connections == set()


def test_manager_disconnect_removes_subscription():
    manager = ConnectionManager()
    websocket = FakeWebSocket()
    manager.active_connections.add(websocket)
    manager.subscribe_orderbook(websocket, "BTCEUR")

    manager.disconnect(websocket)

    assert manager.orderbook_subscriptions == {}


@pytest.mark.parametrize("symbol", ["XRPEUR", "", 42, None])
def test_invalid_symbol_returns_error_and_keeps_connection_open(client, manager, symbol):
    with client.websocket_connect("/ws/market") as websocket:
        websocket.receive_json()
        subscribe(websocket, "BTCEUR")
        subscribe(websocket, symbol)
        error = websocket.receive_json()

        assert error == {"type": "error", "data": {"detail": f"Unknown symbol: {symbol}"}}
        # Still open and still subscribed to the earlier pair.
        websocket.send_json({"action": "unsubscribe_orderbook"})
        subscribe(websocket, "ETHEUR")
        wait_for_server(websocket)
        assert [s.symbol for s in manager.orderbook_subscriptions.values()] == ["ETHEUR"]


def test_oversized_subscribe_message_is_ignored(client, manager):
    with client.websocket_connect("/ws/market") as websocket:
        websocket.receive_json()
        websocket.send_json(
            {"action": "subscribe_orderbook", "symbol": "BTCEUR", "padding": "x" * 1024}
        )
        wait_for_server(websocket)

        assert manager.orderbook_subscriptions == {}
