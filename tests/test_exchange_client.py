import asyncio
import json
from decimal import Decimal

import pytest

from app.services.exchange_client import (
    BinanceStreamClient,
    build_stream_url,
    compute_backoff,
)

ALLOWED = {"BTCUSDT", "ETHUSDT"}


def ticker_message(symbol, price):
    data = {"e": "24hrTicker", "E": 1760380800000, "s": symbol, "c": price}
    return json.dumps({"stream": f"{symbol.lower()}@ticker", "data": data})


class FakeConnection:
    """Returns the queued messages, then fails like a dropped network connection."""

    def __init__(self, messages):
        self.messages = list(messages)
        self.closed = asyncio.Event()

    async def recv(self):
        if self.messages:
            return self.messages.pop(0)
        if self.closed.is_set():
            raise ConnectionError("closed")
        raise ConnectionResetError("connection dropped")

    async def close(self):
        self.closed.set()


class FakeConnect:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *exc_info):
        return None


class BlockingConnection(FakeConnection):
    """Waits for messages until close() is called, like a quiet but healthy connection."""

    async def recv(self):
        await self.closed.wait()
        raise ConnectionError("closed")


async def wait_until(condition):
    # Give the client task a bounded number of turns on the event loop.
    for _ in range(1000):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was never met")


def make_client(connect_factory, on_ticker=None):
    async def ignore(_event):
        return None

    return BinanceStreamClient(
        "wss://example.invalid/stream",
        on_ticker=on_ticker or ignore,
        on_kline=ignore,
        connect_factory=connect_factory,
        allowed_symbols=ALLOWED,
        receive_timeout=5,
    )


def test_build_stream_url():
    url = build_stream_url("wss://data-stream.binance.vision", ["BTCUSDT", "ETHUSDT"], ["1m", "5m"])
    assert url == (
        "wss://data-stream.binance.vision/stream?streams="
        "btcusdt@ticker/btcusdt@kline_1m/btcusdt@kline_5m/"
        "ethusdt@ticker/ethusdt@kline_1m/ethusdt@kline_5m"
    )


def test_build_stream_url_has_25_streams_for_default_settings():
    url = build_stream_url(
        "wss://data-stream.binance.vision",
        ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "ADAUSDT"],
        ["1m", "5m", "15m", "1h"],
    )
    assert len(url.split("streams=")[1].split("/")) == 25


@pytest.mark.parametrize(
    ("attempt", "expected"),
    [(0, 1.0), (1, 2.0), (2, 4.0), (3, 8.0), (5, 32.0), (6, 60.0), (20, 60.0)],
)
def test_compute_backoff(attempt, expected):
    assert compute_backoff(attempt, 1.0, 60.0) == expected


async def test_client_handles_messages_then_reconnects(monkeypatch):
    received = []
    connections = []
    delays = []

    async def on_ticker(ticker):
        received.append(ticker)

    def connect_factory(_url):
        if not connections:
            connection = FakeConnection(
                [ticker_message("BTCUSDT", "113250.55"), ticker_message("ETHUSDT", "4122.75")]
            )
        else:
            connection = BlockingConnection([])
        connections.append(connection)
        return FakeConnect(connection)

    client = make_client(connect_factory, on_ticker)

    async def fake_sleep(delay):
        delays.append(delay)
        await asyncio.sleep(0)

    monkeypatch.setattr(client, "_sleep_unless_stopped", fake_sleep)

    task = asyncio.create_task(client.run())
    await wait_until(lambda: len(connections) == 2)

    assert [ticker.last_price for ticker in received] == [
        Decimal("113250.55"),
        Decimal("4122.75"),
    ]
    assert client.last_message_at is not None
    assert len(delays) == 1
    assert 1.0 <= delays[0] <= 1.1

    await client.stop()
    await asyncio.wait_for(task, timeout=1)


async def test_backoff_grows_and_resets_after_successful_connect(monkeypatch):
    delays = []
    attempts = 0

    def connect_factory(_url):
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            raise OSError("network is down")
        return FakeConnect(FakeConnection([]))

    client = make_client(connect_factory)

    async def fake_sleep(delay):
        delays.append(delay)
        if len(delays) == 5:
            await client.stop()

    monkeypatch.setattr(client, "_sleep_unless_stopped", fake_sleep)

    await asyncio.wait_for(client.run(), timeout=1)

    rounded = [int(delay) for delay in delays]
    # Three failed connects (1, 2, 4 s); each later successful connect resets the counter.
    assert rounded == [1, 2, 4, 1, 1]


async def test_stop_ends_run_loop():
    connection = BlockingConnection([])
    client = make_client(lambda _url: FakeConnect(connection))

    task = asyncio.create_task(client.run())
    await wait_until(lambda: client.status == "connected")

    await client.stop()
    await asyncio.wait_for(task, timeout=1)

    assert client.status == "stopped"
    assert connection.closed.is_set()


async def test_stop_interrupts_reconnect_wait():
    def connect_factory(_url):
        raise OSError("network is down")

    client = make_client(connect_factory)
    client.reconnect_initial_delay = 60

    task = asyncio.create_task(client.run())
    await wait_until(lambda: client.status == "reconnecting")

    await client.stop()
    await asyncio.wait_for(task, timeout=1)
    assert client.status == "stopped"


async def test_failing_callback_does_not_drop_connection():
    calls = []
    connection = FakeConnection(
        [ticker_message("BTCUSDT", "113250.55"), ticker_message("ETHUSDT", "4122.75")]
    )

    async def on_ticker(ticker):
        calls.append(ticker.symbol)
        if ticker.symbol == "BTCUSDT":
            raise RuntimeError("consumer failed")

    client = make_client(lambda _url: FakeConnect(connection), on_ticker)
    await client._dispatch(connection.messages[0])
    await client._dispatch(connection.messages[1])

    assert calls == ["BTCUSDT", "ETHUSDT"]


async def test_cancelled_error_is_propagated():
    connection = BlockingConnection([])
    client = make_client(lambda _url: FakeConnect(connection))

    task = asyncio.create_task(client.run())
    await wait_until(lambda: client.status == "connected")

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


class DrainingConnection:
    """Like real websockets: close() completes only after queued messages have been read."""

    def __init__(self):
        self.queued_after_close = None
        self.drained = asyncio.Event()

    async def recv(self):
        await asyncio.sleep(0)
        if self.queued_after_close == 0:
            self.drained.set()
            raise ConnectionError("closed")
        if self.queued_after_close is not None:
            self.queued_after_close -= 1
        return ticker_message("BTCUSDT", "113250.55")

    async def close(self):
        # Messages already on the wire still arrive before Binance's close frame.
        self.queued_after_close = 3
        await self.drained.wait()


async def test_stop_keeps_reading_until_connection_is_closed():
    received = []

    async def on_ticker(ticker):
        received.append(ticker)

    connection = DrainingConnection()
    client = make_client(lambda _url: FakeConnect(connection), on_ticker)

    task = asyncio.create_task(client.run())
    await wait_until(lambda: len(received) > 0)
    received_before_stop = len(received)

    await asyncio.wait_for(client.stop(), timeout=1)
    await asyncio.wait_for(task, timeout=1)

    assert client.status == "stopped"
    # Messages drained during shutdown are not passed on.
    assert len(received) == received_before_stop
