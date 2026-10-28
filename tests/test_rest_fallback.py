import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from app.config import Settings
from app.services.exchange_client import (
    BinanceRestClient,
    ExchangeUnavailableError,
    ms_to_datetime,
    parse_rest_ticker,
    parse_ticker,
)
from app.services.market_data_service import MarketDataService
from app.services.rest_fallback import RestFallbackPoller

SETTINGS = Settings(_env_file=None)
ALLOWED = set(SETTINGS.supported_symbols)
CLOSE_TIME_MS = 1760380800000


def rest_entry(**overrides):
    """One /api/v3/ticker/24hr entry as Binance sends it (FULL type, numbers as strings)."""
    data = {
        "symbol": "BTCEUR",
        "priceChange": "-440.10000000",
        "priceChangePercent": "-0.604",
        "weightedAvgPrice": "72601.22000000",
        "prevClosePrice": "72856.95000000",
        "lastPrice": "72416.85000000",
        "lastQty": "0.00100000",
        "bidPrice": "72416.84000000",
        "bidQty": "0.20000000",
        "askPrice": "72416.85000000",
        "askQty": "0.10000000",
        "openPrice": "72856.95000000",
        "highPrice": "73350.00000000",
        "lowPrice": "71820.10000000",
        "volume": "412.55100000",
        "quoteVolume": "29951234.12000000",
        "openTime": CLOSE_TIME_MS - 86_400_000,
        "closeTime": CLOSE_TIME_MS,
        "firstId": 1,
        "lastId": 1000,
        "count": 1000,
    }
    data.update(overrides)
    return data


def ws_ticker(event_ms, price):
    return parse_ticker(
        {"e": "24hrTicker", "E": event_ms, "s": "BTCEUR", "c": price, "b": "1", "a": "2"}
    )


def rest_ticker(close_ms, price):
    return parse_rest_ticker(rest_entry(closeTime=close_ms, lastPrice=price))


def make_service(status="reconnecting"):
    service = MarketDataService(SETTINGS)
    service.client.status = status
    return service


def subscribe_recorder(service):
    events = []

    async def record(event):
        events.append(event)

    service.subscribe(record)
    return events


# ---------- Parsing ----------


def test_rest_ticker_is_parsed_like_stream_ticker():
    ticker = parse_rest_ticker(rest_entry(), ALLOWED)

    assert ticker.symbol == "BTCEUR"
    assert ticker.display_symbol == "BTC/EUR"
    assert ticker.last_price == Decimal("72416.85000000")
    assert ticker.price_change == Decimal("-440.10000000")
    assert ticker.price_change_percent == Decimal("-0.604")
    assert ticker.high_24h == Decimal("73350.00000000")
    assert ticker.low_24h == Decimal("71820.10000000")
    assert ticker.volume_24h == Decimal("412.55100000")
    assert ticker.bid_price == Decimal("72416.84000000")
    assert ticker.ask_price == Decimal("72416.85000000")
    assert ticker.spread == Decimal("0.01")
    assert ticker.spread_percent == Decimal("0.000")
    assert ticker.event_time == ms_to_datetime(CLOSE_TIME_MS)
    assert ticker.source == "rest"


def test_stream_ticker_source_is_ws():
    assert ws_ticker(CLOSE_TIME_MS, "1").source == "ws"


@pytest.mark.parametrize(
    "overrides",
    [
        {"lastPrice": "0"},
        {"lastPrice": "-1.5"},
        {"lastPrice": "abc"},
        {"lastPrice": "NaN"},
        {"lastPrice": 72416.85},  # a float would bring binary rounding errors
        {"lastPrice": None},
        {"closeTime": "1760380800000"},
        {"closeTime": None},
        {"symbol": "DOGEEUR"},
        {"symbol": None},
    ],
)
def test_invalid_rest_ticker_is_dropped(overrides):
    assert parse_rest_ticker(rest_entry(**overrides), ALLOWED) is None


def test_rest_ticker_that_is_not_an_object_is_dropped():
    assert parse_rest_ticker(["BTCEUR"], ALLOWED) is None


def test_invalid_rest_bid_ask_keeps_last_price():
    ticker = parse_rest_ticker(rest_entry(bidPrice="0", askPrice="72416.85"), ALLOWED)

    assert ticker.last_price == Decimal("72416.85000000")
    assert ticker.bid_price is None
    assert ticker.ask_price is None
    assert ticker.spread is None


def test_invalid_optional_rest_fields_become_none_not_zero():
    ticker = parse_rest_ticker(rest_entry(highPrice="0", lowPrice="x", volume=None), ALLOWED)

    assert ticker.high_24h is None
    assert ticker.low_24h is None
    assert ticker.volume_24h is None


async def test_get_tickers_requests_all_symbols_at_once_and_drops_invalid_entries():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json=[rest_entry(), rest_entry(symbol="ETHEUR", lastPrice="0")],
        )

    client = BinanceRestClient(SETTINGS, transport=httpx.MockTransport(handler))
    tickers = await client.get_tickers(["BTCEUR", "ETHEUR"])
    await client.close()

    assert [ticker.symbol for ticker in tickers] == ["BTCEUR"]
    assert len(requests) == 1
    assert requests[0].url.path == "/api/v3/ticker/24hr"
    assert json.loads(requests[0].url.params["symbols"]) == ["BTCEUR", "ETHEUR"]
    assert requests[0].url.params["symbols"] == '["BTCEUR","ETHEUR"]'


@pytest.mark.parametrize("status_code", [418, 429])
async def test_get_tickers_rate_limit_raises_exchange_unavailable(status_code):
    client = BinanceRestClient(
        SETTINGS, transport=httpx.MockTransport(lambda _: httpx.Response(status_code))
    )
    with pytest.raises(ExchangeUnavailableError):
        await client.get_tickers(["BTCEUR"])
    await client.close()


# ---------- MarketDataService with REST data ----------


async def test_older_rest_ticker_does_not_overwrite_newer_stream_ticker():
    service = make_service()
    await service.handle_ticker(ws_ticker(CLOSE_TIME_MS, "72500.00"))

    await service.handle_rest_tickers([rest_ticker(CLOSE_TIME_MS - 1000, "70000.00")])

    stored = service.snapshot()["markets"][0]["ticker"]
    assert stored.last_price == Decimal("72500.00")
    assert stored.source == "ws"


async def test_newer_rest_ticker_replaces_stream_ticker():
    service = make_service()
    await service.handle_ticker(ws_ticker(CLOSE_TIME_MS, "72500.00"))
    events = subscribe_recorder(service)

    await service.handle_rest_tickers([rest_ticker(CLOSE_TIME_MS + 1000, "72600.00")])

    assert [event["type"] for event in events] == ["ticker", "status"]
    assert events[0]["data"].source == "rest"
    assert events[1]["data"] == {"connection_status": "reconnecting", "price_source": "rest"}


async def test_rest_ticker_is_live_while_stream_is_down_and_alerts_see_it_live():
    service = make_service()
    seen_freshness = []

    async def alert_like(event):
        if event["type"] == "ticker":
            seen_freshness.append(service.freshness(event["data"].symbol))

    service.subscribe(alert_like)
    await service.handle_rest_tickers([rest_ticker(CLOSE_TIME_MS, "72600.00")])
    polled_at = datetime.now(UTC)

    assert seen_freshness == ["live"]
    assert service.freshness("BTCEUR", now=polled_at + timedelta(seconds=10)) == "live"
    assert service.freshness("BTCEUR", now=polled_at + timedelta(seconds=16)) == "stale"
    assert service.snapshot()["price_source"] == "rest"


async def test_stream_reconnect_switches_price_source_back_to_ws():
    service = make_service()
    await service.handle_rest_tickers([rest_ticker(CLOSE_TIME_MS, "72600.00")])
    events = subscribe_recorder(service)

    service.client.status = "connected"
    await service.handle_status("connected")

    assert service.price_source == "ws"
    assert events == [
        {"type": "status", "data": {"connection_status": "connected", "price_source": "ws"}}
    ]


async def test_rest_round_finishing_after_reconnect_keeps_ws_source():
    service = make_service(status="connected")

    await service.handle_rest_tickers([rest_ticker(CLOSE_TIME_MS, "72600.00")])

    assert service.price_source == "ws"


# ---------- Poller ----------


class StopLoop(Exception):
    """Raised by the fake sleep to end RestFallbackPoller.run() at a chosen time."""


class FakeTime:
    """Replaces asyncio.sleep and time.monotonic: sleeping only moves the clock forward."""

    def __init__(self, end, on_tick=None):
        self.now = 0.0
        self.end = end
        self.on_tick = on_tick

    def clock(self):
        return self.now

    async def sleep(self, delay):
        self.now += delay
        if self.on_tick is not None:
            self.on_tick(self.now)
        if self.now > self.end:
            raise StopLoop


def make_poller(service, fetch, fake_time):
    return RestFallbackPoller(
        service,
        fetch,
        after_seconds=SETTINGS.rest_fallback_after_seconds,
        interval_seconds=SETTINGS.rest_fallback_interval_seconds,
        sleep=fake_time.sleep,
        clock=fake_time.clock,
    )


def recording_fetch(fake_time, calls, result=None):
    async def fetch():
        calls.append(fake_time.now)
        return result or []

    return fetch


async def test_fallback_does_not_start_before_30_s():
    service = make_service()
    fake_time = FakeTime(end=29)
    calls = []
    poller = make_poller(service, recording_fetch(fake_time, calls), fake_time)

    with pytest.raises(StopLoop):
        await poller.run()

    assert calls == []
    assert poller.active is False


async def test_fallback_starts_after_30_s_and_polls_every_10_s():
    service = make_service()
    fake_time = FakeTime(end=60)
    calls = []
    poller = make_poller(
        service, recording_fetch(fake_time, calls, [rest_ticker(CLOSE_TIME_MS, "1")]), fake_time
    )

    with pytest.raises(StopLoop):
        await poller.run()

    assert calls == [30, 40, 50, 60]
    assert poller.active is True
    assert service.price_source == "rest"


async def test_fallback_stops_when_stream_reconnects():
    service = make_service()

    def reconnect_at_45(now):
        if now == 45:
            service.client.status = "connected"

    fake_time = FakeTime(end=120, on_tick=reconnect_at_45)
    calls = []
    poller = make_poller(service, recording_fetch(fake_time, calls), fake_time)

    with pytest.raises(StopLoop):
        await poller.run()

    assert calls == [30, 40]
    assert poller.active is False


async def test_new_outage_waits_30_s_again():
    service = make_service()

    def flap(now):
        if now == 45:
            service.client.status = "connected"
        if now == 50:
            service.client.status = "reconnecting"

    fake_time = FakeTime(end=85, on_tick=flap)
    calls = []
    poller = make_poller(service, recording_fetch(fake_time, calls), fake_time)

    with pytest.raises(StopLoop):
        await poller.run()

    assert calls == [30, 40, 80]


async def test_rate_limit_skips_round_and_task_keeps_running(caplog):
    service = make_service()
    fake_time = FakeTime(end=50)
    calls = []

    async def fetch():
        calls.append(fake_time.now)
        if len(calls) == 1:
            raise ExchangeUnavailableError("Binance rate limit (HTTP 429)")
        return [rest_ticker(CLOSE_TIME_MS, "72600.00")]

    poller = make_poller(service, fetch, fake_time)

    with pytest.raises(StopLoop):
        await poller.run()

    assert calls == [30, 40, 50]
    assert "HTTP 429" in caplog.text
    assert service.snapshot()["markets"][0]["ticker"].last_price == Decimal("72600.00")


async def test_rate_limit_from_real_rest_client_does_not_stop_task():
    service = make_service()
    fake_time = FakeTime(end=40)
    responses = []

    def handler(_request):
        responses.append(429)
        return httpx.Response(429, headers={"Retry-After": "1"})

    client = BinanceRestClient(SETTINGS, transport=httpx.MockTransport(handler))
    poller = make_poller(service, lambda: client.get_tickers(["BTCEUR"]), fake_time)

    with pytest.raises(StopLoop):
        await poller.run()
    await client.close()

    # The second round hits the client's own Retry-After pause, so Binance is not asked again.
    assert responses == [429]


async def test_cancel_during_rest_request_ends_task():
    service = make_service()
    started = asyncio.Event()

    async def hanging_fetch():
        started.set()
        await asyncio.Event().wait()

    poller = RestFallbackPoller(service, hanging_fetch, after_seconds=0.01, interval_seconds=10)
    poller.CHECK_INTERVAL_SECONDS = 0.01
    task = asyncio.create_task(poller.run())
    await asyncio.wait_for(started.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


async def test_cancel_while_waiting_ends_task():
    service = make_service(status="connected")

    async def unused_fetch():
        return []

    poller = RestFallbackPoller(service, unused_fetch, after_seconds=30, interval_seconds=10)
    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
