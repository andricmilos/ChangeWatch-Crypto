import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.config import Settings
from app.schemas.market import KlineUpdate, MarketTicker, RollingChange
from app.services.exchange_client import (
    ms_to_datetime,
    parse_kline,
    parse_message,
    parse_rolling_ticker,
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


def make_rolling(window="1h", **overrides):
    # Binance also sends p, o, h, l, c, v ... for the window; the parser must ignore them.
    data = {
        "e": f"{window}Ticker",
        "E": 1760380800000,
        "s": "BTCEUR",
        "p": "-150.00",
        "P": "-0.21",
        "c": "72416.85",
        "h": "72600.00",
        "l": "72300.00",
        "v": "35.120",
    }
    data.update(overrides)
    return data


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


def test_bid_and_ask_are_parsed():
    ticker = parse_ticker(make_ticker(b="72416.80", a="72416.90"))

    assert ticker.bid_price == Decimal("72416.80")
    assert ticker.ask_price == Decimal("72416.90")


@pytest.mark.parametrize("bad", ["0", "-1", "abc", None])
def test_invalid_bid_or_ask_is_none_not_zero(bad):
    ticker = parse_ticker(make_ticker(b=bad, a=bad))

    assert ticker.bid_price is None
    assert ticker.ask_price is None


def test_spread_is_computed_from_bid_and_ask():
    ticker = parse_ticker(make_ticker(b="99", a="101"))

    assert ticker.spread == Decimal("2")
    # (101 - 99) / ((101 + 99) / 2) * 100
    assert ticker.spread_percent == Decimal("2.000")


@pytest.mark.parametrize(
    ("bid", "ask", "expected"),
    [
        # 0.0001 / 1.00005 * 100 = 0.0099995 -> rounds half up to 0.010
        ("1", "1.0001", "0.010"),
        # 0.0015 / 100.00075 * 100 = 0.0014999... -> 0.001
        ("100", "100.0015", "0.001"),
        ("72416.80", "72416.90", "0.000"),
    ],
)
def test_spread_percent_is_rounded_to_three_decimals(bid, ask, expected):
    ticker = parse_ticker(make_ticker(b=bid, a=ask))

    assert str(ticker.spread_percent) == expected


def test_equal_bid_and_ask_give_zero_spread():
    ticker = parse_ticker(make_ticker(b="100", a="100"))

    assert ticker.spread == Decimal("0")
    assert ticker.spread_percent == Decimal("0.000")


@pytest.mark.parametrize(
    "book",
    [
        {},
        {"b": "72416.80"},
        {"a": "72416.90"},
        {"b": "abc", "a": "72416.90"},
        {"b": "72416.80", "a": "0"},
        {"b": "-1", "a": "72416.90"},
        {"b": 72416.8, "a": "72416.90"},
        # Crossed book: ask below bid.
        {"b": "72416.90", "a": "72416.80"},
    ],
)
def test_unusable_bid_ask_drops_both_but_keeps_ticker(book, caplog):
    data = make_ticker(**book)

    with caplog.at_level("WARNING", logger="app.services.exchange_client"):
        ticker = parse_ticker(data)

    assert ticker is not None
    assert ticker.bid_price is None
    assert ticker.ask_price is None
    assert ticker.spread is None
    assert ticker.spread_percent is None
    assert "Dropping bid/ask for BTCEUR" in caplog.text
    # Last price and 24h data are unaffected.
    assert ticker.last_price == Decimal("72416.85")
    assert ticker.price_change_percent == Decimal("-0.60")
    assert ticker.high_24h == Decimal("73350.00")
    assert ticker.volume_24h == Decimal("412.551")


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


async def test_bid_ask_change_notifies_even_with_same_last_price():
    service = make_service()
    events = subscribe_recorder(service)

    await service.handle_ticker(ticker_at(1760380800000, b="72416.80", a="72416.90"))
    await service.handle_ticker(ticker_at(1760380801000, b="72416.70", a="72416.90"))

    assert len(events) == 2
    assert service.snapshot()["markets"][0]["ticker"].bid_price == Decimal("72416.70")


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


# ---------- Rolling window (1h / 4h) changes ----------


@pytest.mark.parametrize("window", ["1h", "4h"])
def test_rolling_ticker_is_parsed(window):
    event = parse_message(combined(f"btceur@ticker_{window}", make_rolling(window)), ALLOWED)

    assert isinstance(event, RollingChange)
    assert event.symbol == "BTCEUR"
    assert event.window == window
    assert event.change_percent == Decimal("-0.21")
    assert event.event_time == datetime(2025, 10, 13, 18, 40, tzinfo=UTC)


@pytest.mark.parametrize("bad_percent", [None, "", "abc", "NaN", "Infinity", 1.5, True])
def test_rolling_ticker_with_invalid_percent_is_dropped(bad_percent):
    assert parse_rolling_ticker(make_rolling(P=bad_percent), ALLOWED) is None


def test_rolling_ticker_without_percent_is_dropped():
    data = make_rolling()
    del data["P"]
    assert parse_rolling_ticker(data, ALLOWED) is None


def test_rolling_ticker_with_invalid_event_time_is_dropped():
    assert parse_rolling_ticker(make_rolling(E="soon"), ALLOWED) is None


def test_rolling_ticker_outside_allowlist_is_dropped():
    assert parse_rolling_ticker(make_rolling(s="DOGEEUR"), ALLOWED) is None


def test_rolling_ticker_with_unknown_window_is_ignored():
    assert parse_rolling_ticker(make_rolling("1d"), ALLOWED) is None
    assert parse_message(combined("btceur@ticker_1d", make_rolling("1d")), ALLOWED) is None


def rolling_at(event_ms, percent, window="1h", symbol="BTCEUR"):
    return parse_rolling_ticker(make_rolling(window, E=event_ms, P=percent, s=symbol))


async def test_rolling_change_does_not_touch_price_or_24h_data():
    service = make_service()
    await service.handle_ticker(ticker_at(1760380800000))
    before = service.snapshot()["markets"][0]["ticker"]

    await service.handle_rolling(rolling_at(1760380801000, "1.25"))
    await service.handle_rolling(rolling_at(1760380801000, "-3.40", window="4h"))

    btc = service.snapshot()["markets"][0]
    assert btc["ticker"] == before
    assert btc["ticker"].last_price == Decimal("72416.85")
    assert btc["ticker"].price_change_percent == Decimal("-0.60")
    assert btc["change_1h_percent"] == Decimal("1.25")
    assert btc["change_4h_percent"] == Decimal("-3.40")


async def test_rolling_change_only_sets_its_own_window():
    service = make_service()

    await service.handle_rolling(rolling_at(1760380800000, "1.25", window="4h"))

    btc = service.snapshot()["markets"][0]
    assert btc["change_1h_percent"] is None
    assert btc["change_4h_percent"] == Decimal("1.25")


async def test_older_rolling_change_does_not_overwrite_newer():
    service = make_service()
    events = subscribe_recorder(service)

    await service.handle_rolling(rolling_at(1760380801000, "1.25"))
    await service.handle_rolling(rolling_at(1760380800000, "0.90"))

    assert service.snapshot()["markets"][0]["change_1h_percent"] == Decimal("1.25")
    assert len(events) == 1


async def test_rolling_change_notifies_only_when_value_changes():
    service = make_service()
    events = subscribe_recorder(service)

    await service.handle_rolling(rolling_at(1760380800000, "1.25"))
    await service.handle_rolling(rolling_at(1760380801000, "1.25"))
    await service.handle_rolling(rolling_at(1760380802000, "1.30"))

    assert [event["type"] for event in events] == ["rolling", "rolling"]
    assert [event["data"].change_percent for event in events] == [
        Decimal("1.25"),
        Decimal("1.30"),
    ]
