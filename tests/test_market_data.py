import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.schemas.market import KlineUpdate, MarketTicker
from app.services.exchange_client import (
    ms_to_datetime,
    parse_kline,
    parse_message,
    parse_ticker,
    to_display_symbol,
)

ALLOWED = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "ADAUSDT"}


def make_ticker(**overrides):
    data = {
        "e": "24hrTicker",
        "E": 1760380800000,
        "s": "BTCUSDT",
        "p": "-512.10",
        "P": "-0.45",
        "c": "113250.55",
        "h": "114900.00",
        "l": "112600.10",
        "v": "18234.551",
    }
    data.update(overrides)
    return data


def make_kline(is_closed):
    return {
        "e": "kline",
        "E": 1760380860000,
        "s": "ETHUSDT",
        "k": {
            "t": 1760380800000,
            "T": 1760380859999,
            "s": "ETHUSDT",
            "i": "1m",
            "o": "4120.10",
            "h": "4125.00",
            "l": "4118.35",
            "c": "4122.75",
            "v": "152.3301",
            "x": is_closed,
        },
    }


def combined(stream, data):
    return json.dumps({"stream": stream, "data": data})


def test_to_display_symbol():
    assert to_display_symbol("BTCUSDT") == "BTC/USDT"
    assert to_display_symbol("ADAUSDT") == "ADA/USDT"


def test_ms_to_datetime_is_utc():
    assert ms_to_datetime(1760380800000) == datetime(2025, 10, 13, 18, 40, tzinfo=UTC)


def test_valid_ticker_has_exact_decimal_values():
    ticker = parse_ticker(make_ticker())

    assert isinstance(ticker, MarketTicker)
    assert ticker.symbol == "BTCUSDT"
    assert ticker.display_symbol == "BTC/USDT"
    assert ticker.last_price == Decimal("113250.55")
    assert ticker.price_change == Decimal("-512.10")
    assert ticker.price_change_percent == Decimal("-0.45")
    assert ticker.high_24h == Decimal("114900.00")
    assert ticker.low_24h == Decimal("112600.10")
    assert ticker.volume_24h == Decimal("18234.551")
    assert ticker.event_time == ms_to_datetime(1760380800000)
    assert ticker.event_time.tzinfo == UTC


def test_combined_stream_format_is_unwrapped():
    ticker = parse_message(combined("btcusdt@ticker", make_ticker()), ALLOWED)

    assert isinstance(ticker, MarketTicker)
    assert ticker.last_price == Decimal("113250.55")


def test_missing_last_price_returns_none():
    data = make_ticker()
    del data["c"]
    assert parse_message(combined("btcusdt@ticker", data), ALLOWED) is None


@pytest.mark.parametrize("bad_price", ["abc", "0", "-1", "", "NaN", None])
def test_invalid_last_price_returns_none(bad_price):
    data = make_ticker(c=bad_price)
    assert parse_message(combined("btcusdt@ticker", data), ALLOWED) is None


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
    data = make_ticker(s="DOGEUSDT")
    assert parse_message(combined("dogeusdt@ticker", data), ALLOWED) is None


def test_open_kline():
    kline = parse_message(combined("ethusdt@kline_1m", make_kline(False)), ALLOWED)

    assert isinstance(kline, KlineUpdate)
    assert kline.symbol == "ETHUSDT"
    assert kline.interval == "1m"
    assert kline.open_time == ms_to_datetime(1760380800000)
    assert kline.close_time == ms_to_datetime(1760380859999)
    assert kline.open == Decimal("4120.10")
    assert kline.high == Decimal("4125.00")
    assert kline.low == Decimal("4118.35")
    assert kline.close == Decimal("4122.75")
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
    data["s"] = data["k"]["s"] = "DOGEUSDT"
    assert parse_message(combined("dogeusdt@kline_1m", data), ALLOWED) is None


@pytest.mark.parametrize("raw", ["not json", "{", "", "[1, 2]", "null", '{"data": "x"}'])
def test_invalid_json_returns_none(raw):
    assert parse_message(raw, ALLOWED) is None


def test_unknown_message_type_is_ignored():
    data = {"e": "aggTrade", "s": "BTCUSDT", "p": "113250.55"}
    assert parse_message(combined("btcusdt@aggTrade", data), ALLOWED) is None
