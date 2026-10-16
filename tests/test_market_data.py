import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.config import Settings
from app.schemas.market import KlineUpdate, MarketTicker
from app.services.exchange_client import (
    ms_to_datetime,
    parse_kline,
    parse_message,
    parse_ticker,
    quote_asset,
    to_display_symbol,
)
from app.services.market_data_service import MarketDataService

ALLOWED = {"BTCEUR", "ETHEUR", "SOLEUR", "BNBEUR", "ADAEUR"}


def make_ticker(**overrides):
    data = {
        "e": "24hrTicker",
        "E": 1760380800000,
        "s": "BTCEUR",
        "p": "-440.10",
        "P": "-0.60",
        "c": "72416.85",
        "h": "73350.00",
        "l": "71820.10",
        "v": "412.551",
    }
    data.update(overrides)
    return data


def make_kline(is_closed):
    return {
        "e": "kline",
        "E": 1760380860000,
        "s": "ETHEUR",
        "k": {
            "t": 1760380800000,
            "T": 1760380859999,
            "s": "ETHEUR",
            "i": "1m",
            "o": "2143.10",
            "h": "2148.00",
            "l": "2141.35",
            "c": "2145.75",
            "v": "152.3301",
            "x": is_closed,
        },
    }


def combined(stream, data):
    return json.dumps({"stream": stream, "data": data})


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("BTCEUR", "BTC/EUR"),
        ("ADAEUR", "ADA/EUR"),
        ("BTCUSDT", "BTC/USDT"),
        ("ETHBTC", "ETH/BTC"),
        ("UNKNOWN", "UNKNOWN"),
    ],
)
def test_to_display_symbol_recognises_known_quote_currencies(symbol, expected):
    assert to_display_symbol(symbol) == expected


def test_quote_asset():
    assert quote_asset("SOLEUR") == "EUR"
    assert quote_asset("SOLUSDT") == "USDT"
    assert quote_asset("EUR") is None


def test_ms_to_datetime_is_utc():
    assert ms_to_datetime(1760380800000) == datetime(2025, 10, 13, 18, 40, tzinfo=UTC)


def test_valid_ticker_has_exact_decimal_values():
    ticker = parse_ticker(make_ticker())

    assert isinstance(ticker, MarketTicker)
    assert ticker.symbol == "BTCEUR"
    assert ticker.display_symbol == "BTC/EUR"
    assert ticker.last_price == Decimal("72416.85")
    assert ticker.price_change == Decimal("-440.10")
    assert ticker.price_change_percent == Decimal("-0.60")
    assert ticker.high_24h == Decimal("73350.00")
    assert ticker.low_24h == Decimal("71820.10")
    assert ticker.volume_24h == Decimal("412.551")
    assert ticker.event_time == ms_to_datetime(1760380800000)
    assert ticker.event_time.tzinfo == UTC


def test_combined_stream_format_is_unwrapped():
    ticker = parse_message(combined("btceur@ticker", make_ticker()), ALLOWED)

    assert isinstance(ticker, MarketTicker)
    assert ticker.last_price == Decimal("72416.85")


def test_missing_last_price_returns_none():
    data = make_ticker()
    del data["c"]
    assert parse_message(combined("btceur@ticker", data), ALLOWED) is None


@pytest.mark.parametrize("bad_price", ["abc", "0", "-1", "", "NaN", None])
def test_invalid_last_price_returns_none(bad_price):
    data = make_ticker(c=bad_price)
    assert parse_message(combined("btceur@ticker", data), ALLOWED) is None


def test_invalid_last_price_is_logged(caplog):
    parse_ticker(make_ticker(c="abc"))
    assert "invalid last price" in caplog.text


def test_missing_optional_field_is_none_not_zero():
    data = make_ticker(h="abc")
    del data["v"]
    ticker = parse_ticker(data)

    assert ticker is not None
    assert ticker.high_24h is None
    assert ticker.volume_24h is None


def test_symbol_outside_allowlist_returns_none():
    data = make_ticker(s="DOGEEUR")
    assert parse_message(combined("dogeeur@ticker", data), ALLOWED) is None


def test_open_kline():
    kline = parse_message(combined("etheur@kline_1m", make_kline(False)), ALLOWED)

    assert isinstance(kline, KlineUpdate)
    assert kline.symbol == "ETHEUR"
    assert kline.interval == "1m"
    assert kline.open_time == ms_to_datetime(1760380800000)
    assert kline.close_time == ms_to_datetime(1760380859999)
    assert kline.open == Decimal("2143.10")
    assert kline.high == Decimal("2148.00")
    assert kline.low == Decimal("2141.35")
    assert kline.close == Decimal("2145.75")
    assert kline.volume == Decimal("152.3301")
    assert kline.is_closed is False


def test_closed_kline():
    kline = parse_kline(make_kline(True))

    assert kline is not None
    assert kline.is_closed is True


def test_kline_with_invalid_close_returns_none():
    data = make_kline(False)
    data["k"]["c"] = "0"
    assert parse_kline(data) is None


def test_kline_outside_allowlist_returns_none():
    data = make_kline(True)
    data["s"] = data["k"]["s"] = "DOGEEUR"
    assert parse_message(combined("dogeeur@kline_1m", data), ALLOWED) is None


@pytest.mark.parametrize("raw", ["not json", "{", "", "[1, 2]", "null", '{"data": "x"}'])
def test_invalid_json_returns_none(raw):
    assert parse_message(raw, ALLOWED) is None


def test_unknown_message_type_is_ignored():
    data = {"e": "aggTrade", "s": "BTCEUR", "p": "72416.85"}
    assert parse_message(combined("btceur@aggTrade", data), ALLOWED) is None


def make_service():
    service = MarketDataService(Settings(_env_file=None))
    # No real connection in tests; pretend the stream is up.
    service.client.status = "connected"
    # Freshness follows the connection: it counts as live while any stream sends messages.
    service.client.last_message_at = datetime.now(UTC)
    return service


def ticker_at(event_ms, price="72416.85", **overrides):
    return parse_ticker(make_ticker(E=event_ms, c=price, **overrides))


def subscribe_recorder(service):
    events = []

    async def record(event):
        events.append(event)

    service.subscribe(record)
    return events


async def test_newer_ticker_replaces_older():
    service = make_service()
    events = subscribe_recorder(service)

    await service.handle_ticker(ticker_at(1760380800000, "72416.85"))
    await service.handle_ticker(ticker_at(1760380801000, "72480.00"))

    market = service.snapshot()["markets"][0]
    assert market["ticker"].last_price == Decimal("72480.00")
    assert [event["data"].last_price for event in events] == [
        Decimal("72416.85"),
        Decimal("72480.00"),
    ]


async def test_older_ticker_is_ignored():
    service = make_service()
    events = subscribe_recorder(service)

    await service.handle_ticker(ticker_at(1760380801000, "72480.00"))
    await service.handle_ticker(ticker_at(1760380800000, "72416.85"))

    assert service.snapshot()["markets"][0]["ticker"].last_price == Decimal("72480.00")
    assert len(events) == 1


async def test_same_data_does_not_notify_but_refreshes_update_time():
    service = make_service()
    events = subscribe_recorder(service)

    await service.handle_ticker(ticker_at(1760380800000))
    first_update = service.snapshot()["markets"][0]["updated_at"]
    await service.handle_ticker(ticker_at(1760380801000))

    assert len(events) == 1
    assert service.snapshot()["markets"][0]["updated_at"] >= first_update


async def test_freshness_live_then_stale_after_threshold():
    service = make_service()
    await service.handle_ticker(ticker_at(1760380800000))
    last_message_at = service.client.last_message_at

    assert service.freshness("BTCEUR", now=last_message_at + timedelta(seconds=10)) == "live"
    assert service.freshness("BTCEUR", now=last_message_at + timedelta(seconds=16)) == "stale"


async def test_quiet_pair_stays_live_while_connection_receives_messages():
    # ADAEUR can go minutes without a trade; other streams keep the connection busy.
    service = make_service()
    ada = ticker_at(1760380800000, "0.1986", s="ADAEUR", p="-0.0041", h="0.2050", l="0.1942")
    await service.handle_ticker(ada)
    ada_updated_at = service.snapshot()["markets"][4]["updated_at"]
    service.client.last_message_at = ada_updated_at + timedelta(seconds=120)

    now = ada_updated_at + timedelta(seconds=125)
    assert service.freshness("ADAEUR", now=now) == "live"


async def test_freshness_is_stale_without_any_message_on_connection():
    service = make_service()
    await service.handle_ticker(ticker_at(1760380800000))
    service.client.last_message_at = None

    assert service.freshness("BTCEUR") == "stale"


async def test_freshness_is_stale_when_client_not_connected():
    service = make_service()
    await service.handle_ticker(ticker_at(1760380800000))
    service.client.status = "reconnecting"

    assert service.freshness("BTCEUR") == "stale"


def test_freshness_no_data_without_ticker():
    service = make_service()
    assert service.freshness("ETHEUR") == "no_data"


def test_snapshot_lists_all_symbols_without_data():
    service = make_service()
    snapshot = service.snapshot()

    assert snapshot["connection_status"] == "connected"
    assert [market["symbol"] for market in snapshot["markets"]] == [
        "BTCEUR",
        "ETHEUR",
        "SOLEUR",
        "BNBEUR",
        "ADAEUR",
    ]
    eth = snapshot["markets"][1]
    assert eth["display_symbol"] == "ETH/EUR"
    assert eth["ticker"] is None
    assert eth["freshness"] == "no_data"


async def test_failing_subscriber_does_not_break_others():
    service = make_service()

    async def broken(_event):
        raise RuntimeError("subscriber failed")

    service.subscribe(broken)
    events = subscribe_recorder(service)

    await service.handle_ticker(ticker_at(1760380800000))

    assert len(events) == 1


async def test_kline_is_stored_and_published():
    service = make_service()
    events = subscribe_recorder(service)

    await service.handle_kline(parse_kline(make_kline(False)))

    assert events[0]["type"] == "kline"
    assert events[0]["data"].close == Decimal("2145.75")


async def test_start_and_stop_finish_client_task(monkeypatch):
    service = make_service()

    async def fake_run():
        await service.client._stop_event.wait()

    monkeypatch.setattr(service.client, "run", fake_run)

    await service.start()
    await service.stop()

    assert service._task is None
