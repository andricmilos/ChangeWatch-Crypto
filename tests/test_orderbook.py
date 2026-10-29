import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.dependencies import get_market_service, get_orderbook_service
from app.config import Settings
from app.main import app
from app.schemas.orderbook import OrderBookSnapshot
from app.services.exchange_client import (
    BinanceStreamClient,
    RejectedDepth,
    build_stream_url,
    imbalance_percent,
    parse_message,
)
from app.services.market_data_service import MarketDataService
from app.services.orderbook_service import OrderBookService
from app.services.runtime_stats import RuntimeStats
from tests.test_exchange_client import FakeConnect, ticker_message, wait_until
from tests.test_runtime_stats import QueuedConnection

ALLOWED = {"BTCEUR", "ETHEUR"}
RECEIVED_AT = datetime(2026, 10, 29, 12, 0, 0, tzinfo=UTC)


def make_depth(**overrides):
    data = {
        "lastUpdateId": 160,
        "bids": [["100.00", "1.5"], ["99.50", "2.0"], ["99.00", "0.5"]],
        "asks": [["100.10", "1.0"], ["100.20", "0.25"], ["100.50", "2.0"]],
    }
    data.update(overrides)
    return data


def depth_message(data=None, stream="btceur@depth10"):
    return json.dumps({"stream": stream, "data": make_depth() if data is None else data})


def parse_depth_message(data=None, stream="btceur@depth10", received_at=RECEIVED_AT):
    return parse_message(depth_message(data, stream), ALLOWED, received_at)


def make_snapshot(last_update_id=160, received_at=RECEIVED_AT, symbol="BTCEUR"):
    snapshot = parse_depth_message(
        make_depth(lastUpdateId=last_update_id), f"{symbol.lower()}@depth10", received_at
    )
    assert isinstance(snapshot, OrderBookSnapshot)
    return snapshot


# ---------- Parsing ----------


def test_combined_depth_message_takes_symbol_from_stream_name():
    snapshot = parse_depth_message(stream="etheur@depth10")

    assert isinstance(snapshot, OrderBookSnapshot)
    assert snapshot.symbol == "ETHEUR"
    assert snapshot.last_update_id == 160
    assert snapshot.received_at == RECEIVED_AT


def test_received_at_defaults_to_now_in_utc():
    before = datetime.now(UTC)
    snapshot = parse_message(depth_message(), ALLOWED)

    assert isinstance(snapshot, OrderBookSnapshot)
    assert snapshot.received_at.tzinfo == UTC
    assert before <= snapshot.received_at <= datetime.now(UTC)


def test_depth_levels_are_exact_decimals_with_cumulative_quantities():
    snapshot = parse_depth_message()

    assert [
        (level.price, level.quantity, level.cumulative_quantity) for level in snapshot.bids
    ] == [
        (Decimal("100.00"), Decimal("1.5"), Decimal("1.5")),
        (Decimal("99.50"), Decimal("2.0"), Decimal("3.5")),
        (Decimal("99.00"), Decimal("0.5"), Decimal("4.0")),
    ]
    assert [
        (level.price, level.quantity, level.cumulative_quantity) for level in snapshot.asks
    ] == [
        (Decimal("100.10"), Decimal("1.0"), Decimal("1.0")),
        (Decimal("100.20"), Decimal("0.25"), Decimal("1.25")),
        (Decimal("100.50"), Decimal("2.0"), Decimal("3.25")),
    ]


def test_derived_fields_are_exact():
    snapshot = parse_depth_message()

    assert snapshot.best_bid == Decimal("100.00")
    assert snapshot.best_ask == Decimal("100.10")
    assert snapshot.spread == Decimal("0.10")
    assert snapshot.mid_price == Decimal("100.05")
    # 0.10 / 100.05 * 100 = 0.09995…, rounded half up to 3 decimals.
    assert snapshot.spread_percent == Decimal("0.100")
    assert snapshot.bid_volume == Decimal("4.0")
    assert snapshot.ask_volume == Decimal("3.25")
    # (4.0 - 3.25) / 7.25 * 100 = 10.3448…
    assert snapshot.imbalance_percent == Decimal("10.34")


def test_imbalance_rounds_half_up_to_two_decimals():
    # 24.69 / 200 * 100 = 12.345 exactly; banker's rounding would give 12.34.
    assert imbalance_percent(Decimal("112.345"), Decimal("87.655")) == Decimal("12.35")
    assert imbalance_percent(Decimal("87.655"), Decimal("112.345")) == Decimal("-12.35")


def test_imbalance_is_none_when_both_volumes_are_zero():
    snapshot = parse_depth_message(make_depth(bids=[["100.00", "0"]], asks=[["100.10", "0"]]))

    assert isinstance(snapshot, OrderBookSnapshot)
    assert snapshot.bid_volume == Decimal("0")
    assert snapshot.imbalance_percent is None


def test_zero_quantity_is_allowed():
    snapshot = parse_depth_message(make_depth(bids=[["100.00", "0"], ["99.00", "1"]]))

    assert isinstance(snapshot, OrderBookSnapshot)
    assert snapshot.bids[0].quantity == Decimal("0")


@pytest.mark.parametrize(
    ("case", "data", "stream"),
    [
        ("unsupported symbol", make_depth(), "dogeeur@depth10"),
        ("stream without symbol", make_depth(), "@depth10"),
        ("unknown depth", make_depth(), "btceur@depth7"),
        ("missing bids", {"lastUpdateId": 160, "asks": make_depth()["asks"]}, None),
        ("missing asks", {"lastUpdateId": 160, "bids": make_depth()["bids"]}, None),
        ("empty bids", make_depth(bids=[]), None),
        ("bids not a list", make_depth(bids="100.00"), None),
        ("level not a pair", make_depth(asks=[["100.10"]]), None),
        ("price not a number", make_depth(bids=[["abc", "1"]]), None),
        ("quantity not a number", make_depth(asks=[["100.10", "x"]]), None),
        ("price NaN", make_depth(bids=[["NaN", "1"]]), None),
        ("price as float", make_depth(bids=[[100.0, "1"]]), None),
        ("price null", make_depth(asks=[[None, "1"]]), None),
        ("price zero", make_depth(bids=[["100.00", "1"], ["0", "1"]]), None),
        ("price negative", make_depth(bids=[["-1", "1"]]), None),
        ("quantity negative", make_depth(asks=[["100.10", "-0.1"]]), None),
        ("bids ascending", make_depth(bids=[["99.00", "1"], ["100.00", "1"]]), None),
        ("bids repeated price", make_depth(bids=[["100.00", "1"], ["100.00", "1"]]), None),
        ("asks descending", make_depth(asks=[["100.50", "1"], ["100.10", "1"]]), None),
        ("asks repeated price", make_depth(asks=[["100.10", "1"], ["100.10", "1"]]), None),
        ("crossed book", make_depth(bids=[["100.20", "1"]], asks=[["100.10", "1"]]), None),
        ("locked book", make_depth(bids=[["100.10", "1"]], asks=[["100.10", "1"]]), None),
        (
            "missing lastUpdateId",
            {"bids": make_depth()["bids"], "asks": make_depth()["asks"]},
            None,
        ),
        ("lastUpdateId as string", make_depth(lastUpdateId="160"), None),
    ],
)
def test_invalid_depth_is_rejected_and_logged(caplog, case, data, stream):
    with caplog.at_level(logging.WARNING, logger="app.services.exchange_client"):
        result = parse_depth_message(data, stream or "btceur@depth10")

    assert isinstance(result, RejectedDepth), case
    assert any(record.levelno == logging.WARNING for record in caplog.records), case


def test_ticker_message_is_not_mistaken_for_depth():
    event = parse_message(ticker_message("BTCEUR", "72416.85"), ALLOWED)

    assert not isinstance(event, OrderBookSnapshot | RejectedDepth)


# ---------- Stream URL and settings ----------


def test_build_stream_url_adds_depth_stream_per_pair():
    url = build_stream_url(
        "wss://data-stream.binance.vision", ["BTCEUR", "ETHEUR"], ["1m"], depth_levels=10
    )

    assert url == (
        "wss://data-stream.binance.vision/stream?streams="
        "btceur@ticker/btceur@kline_1m/btceur@depth10/"
        "etheur@ticker/etheur@kline_1m/etheur@depth10"
    )


def test_stream_url_includes_depth_when_orderbook_is_enabled():
    service = MarketDataService(Settings(_env_file=None))

    streams = service.client.url.split("streams=")[1].split("/")
    assert [s for s in streams if "@depth" in s] == [
        "btceur@depth10",
        "etheur@depth10",
        "soleur@depth10",
        "bnbeur@depth10",
        "adaeur@depth10",
    ]
    # No 100 ms variant: one update per second is enough and keeps traffic low.
    assert "@100ms" not in service.client.url
    assert len(streams) == 40


def test_stream_url_uses_configured_levels():
    service = MarketDataService(Settings(_env_file=None, orderbook_levels=20))

    assert "btceur@depth20" in service.client.url


def test_stream_url_has_no_depth_when_orderbook_is_disabled():
    service = MarketDataService(Settings(_env_file=None, orderbook_enabled=False))

    assert "@depth" not in service.client.url


def test_orderbook_settings_defaults():
    settings = Settings(_env_file=None)

    assert settings.orderbook_enabled is True
    assert settings.orderbook_levels == 10
    assert settings.orderbook_stale_seconds == 5


@pytest.mark.parametrize("levels", [0, 1, 7, 50])
def test_orderbook_levels_must_be_5_10_or_20(levels):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, orderbook_levels=levels)


@pytest.mark.parametrize("seconds", [0, -1])
def test_orderbook_stale_seconds_must_be_positive(seconds):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, orderbook_stale_seconds=seconds)


# ---------- OrderBookService ----------


async def test_service_keeps_latest_snapshot_and_ignores_older_update_id():
    service = OrderBookService(Settings(_env_file=None))
    newer = make_snapshot(last_update_id=200)
    older = make_snapshot(last_update_id=150)

    await service.handle_snapshot(newer)
    await service.handle_snapshot(older)

    assert service.get("BTCEUR") is newer


async def test_service_replaces_with_newer_update_id():
    service = OrderBookService(Settings(_env_file=None))
    await service.handle_snapshot(make_snapshot(last_update_id=150))
    newer = make_snapshot(last_update_id=151)

    await service.handle_snapshot(newer)

    assert service.get("BTCEUR") is newer


async def test_update_ids_are_tracked_per_pair():
    service = OrderBookService(Settings(_env_file=None))
    await service.handle_snapshot(make_snapshot(last_update_id=500, symbol="BTCEUR"))
    eth = make_snapshot(last_update_id=10, symbol="ETHEUR")

    await service.handle_snapshot(eth)

    assert service.get("ETHEUR") is eth


async def test_freshness_live_stale_and_no_data():
    service = OrderBookService(Settings(_env_file=None))
    await service.handle_snapshot(make_snapshot())

    assert service.freshness("ETHEUR", RECEIVED_AT) == "no_data"
    assert service.freshness("BTCEUR", RECEIVED_AT + timedelta(seconds=5)) == "live"
    assert service.freshness("BTCEUR", RECEIVED_AT + timedelta(seconds=5.1)) == "stale"


async def test_view_without_data_has_empty_lists_and_nulls():
    service = OrderBookService(Settings(_env_file=None))

    view = service.view("BTCEUR")

    assert view.freshness == "no_data"
    assert view.bids == []
    assert view.asks == []
    assert view.best_bid is None
    assert view.imbalance_percent is None


# ---------- Stream client ----------


def make_stream_client(connection, stats, on_ticker, on_depth):
    async def ignore(_event):
        return None

    return BinanceStreamClient(
        "wss://example.invalid/stream",
        on_ticker=on_ticker,
        on_kline=ignore,
        on_depth=on_depth,
        connect_factory=lambda _url: FakeConnect(connection),
        allowed_symbols=ALLOWED,
        receive_timeout=5,
        stats=stats,
    )


async def test_client_counts_depth_messages_and_a_failing_handler_does_not_stop_prices():
    stats = RuntimeStats()
    tickers = []
    depth_calls = []

    async def on_ticker(ticker):
        tickers.append(ticker)

    async def failing_depth(snapshot):
        depth_calls.append(snapshot)
        raise RuntimeError("order book handler broke")

    connection = QueuedConnection(
        [
            depth_message(),
            depth_message(make_depth(bids=[])),
            ticker_message("BTCEUR", "72416.85"),
        ]
    )
    client = make_stream_client(connection, stats, on_ticker, failing_depth)

    task = asyncio.create_task(client.run())
    await wait_until(lambda: stats.messages_total == 3)

    assert len(depth_calls) == 1
    assert [ticker.last_price for ticker in tickers] == [Decimal("72416.85")]
    assert stats.depth_messages_total == 2
    assert stats.depth_messages_rejected == 1
    assert stats.messages_rejected == 1
    assert stats.snapshot()["orderbook"] == {"received": 2, "rejected": 1}
    # Depth messages do not count as pair activity for seconds_since_last_message.
    assert stats.seconds_since_pair_message("BTCEUR") is not None
    await client.stop()
    await asyncio.wait_for(task, timeout=1)


# ---------- REST endpoint ----------


@pytest.fixture
async def orderbook_service():
    service = OrderBookService(Settings(_env_file=None))
    await service.handle_snapshot(make_snapshot(received_at=datetime.now(UTC)))
    return service


@pytest.fixture
def client(orderbook_service):
    market_service = MarketDataService(Settings(_env_file=None))  # never started
    original_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_market_service] = lambda: market_service
    app.dependency_overrides[get_orderbook_service] = lambda: orderbook_service
    # No `with` block: the lifespan (and the real Binance client) does not start.
    yield TestClient(app)
    app.dependency_overrides.clear()
    app.dependency_overrides.update(original_overrides)


def test_orderbook_endpoint_returns_snapshot_with_strings(client):
    response = client.get("/api/markets/btc-eur/orderbook")

    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "BTCEUR"
    assert body["display_symbol"] == "BTC/EUR"
    assert body["freshness"] == "live"
    assert body["last_update_id"] == 160
    assert body["received_at"] is not None
    assert body["bids"][0] == {"price": "100.00", "quantity": "1.5", "cumulative_quantity": "1.5"}
    assert body["asks"][-1] == {
        "price": "100.50",
        "quantity": "2.0",
        "cumulative_quantity": "3.25",
    }
    assert body["best_bid"] == "100.00"
    assert body["best_ask"] == "100.10"
    assert body["spread"] == "0.10"
    assert body["spread_percent"] == "0.100"
    assert body["mid_price"] == "100.05"
    assert body["bid_volume"] == "4.0"
    assert body["ask_volume"] == "3.25"
    assert body["imbalance_percent"] == "10.34"


def test_orderbook_endpoint_without_data_returns_no_data(client):
    response = client.get("/api/markets/ETHEUR/orderbook")

    assert response.status_code == 200
    assert response.json() == {
        "symbol": "ETHEUR",
        "display_symbol": "ETH/EUR",
        "freshness": "no_data",
        "last_update_id": None,
        "received_at": None,
        "bids": [],
        "asks": [],
        "best_bid": None,
        "best_ask": None,
        "spread": None,
        "spread_percent": None,
        "mid_price": None,
        "bid_volume": None,
        "ask_volume": None,
        "imbalance_percent": None,
    }


async def test_orderbook_endpoint_reports_stale(client, orderbook_service):
    old = datetime.now(UTC) - timedelta(seconds=30)
    await orderbook_service.handle_snapshot(make_snapshot(last_update_id=161, received_at=old))

    assert client.get("/api/markets/BTCEUR/orderbook").json()["freshness"] == "stale"


def test_orderbook_endpoint_unknown_symbol_is_404(client):
    response = client.get("/api/markets/DOGEEUR/orderbook")

    assert response.status_code == 404
    assert response.json() == {"detail": "Unknown symbol: DOGEEUR"}
