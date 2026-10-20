from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_history_service, get_market_service
from app.config import Settings
from app.main import app
from app.services.exchange_client import (
    BinanceRestClient,
    datetime_to_ms,
    ms_to_datetime,
    parse_ticker,
)
from app.services.history_service import HistoryService
from app.services.market_data_service import MarketDataService

SETTINGS = Settings(_env_file=None)
NOW = datetime(2026, 10, 18, 12, 30, 15, tzinfo=UTC)
HOUR_MS = 3_600_000
# BNBEUR "was listed" on this date; Binance has no candles for it before then.
BNB_LISTED_AT = datetime(2026, 10, 1, tzinfo=UTC)


class FakeBinance:
    """httpx.MockTransport handler for /api/v3/klines with startTime and limit=1.

    Like Binance, it returns the first 1h candle opening at or after startTime; every
    candle opens at `open_prices[symbol]`.
    """

    def __init__(self, open_prices=None, listed_at=None):
        self.open_prices = open_prices or {
            "BTCEUR": "50000",
            "ETHEUR": "2000",
            "SOLEUR": "200",
            "BNBEUR": "500",
            "ADAEUR": "0.25",
        }
        self.listed_at = listed_at or {"BNBEUR": BNB_LISTED_AT}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        params = request.url.params
        assert params["interval"] == "1h"
        assert params["limit"] == "1"
        symbol = params["symbol"]
        open_ms = int(params["startTime"])
        open_ms += -open_ms % HOUR_MS
        listed_at = self.listed_at.get(symbol)
        if listed_at is not None:
            open_ms = max(open_ms, datetime_to_ms(listed_at))
        if symbol not in self.open_prices:
            return httpx.Response(200, json=[])
        price = self.open_prices[symbol]
        row = [open_ms, price, price, price, price, "1.0", open_ms + HOUR_MS - 1, "0", 1]
        return httpx.Response(200, json=[row])

    def start_times(self):
        return [ms_to_datetime(int(r.url.params["startTime"])) for r in self.requests]


def ticker(symbol, last, bid=None, ask=None):
    data = {"e": "24hrTicker", "E": 1760380800000, "s": symbol, "c": last}
    if bid is not None:
        data["b"] = bid
        data["a"] = ask or bid
    return parse_ticker(data)


async def make_market_service(*tickers):
    # Never started, so there is no Binance connection; data is written in directly.
    service = MarketDataService(SETTINGS)
    service.client.status = "connected"
    service.client.last_message_at = datetime.now(UTC)
    for item in tickers:
        await service.handle_ticker(item)
    return service


DEFAULT_TICKERS = (
    # Bid differs from last, so the tests can tell which one was used.
    ticker("BTCEUR", "72500.00", bid="72416.85", ask="72417.00"),
    # No order book: the last price is used.
    ticker("ETHEUR", "2143.10"),
    ticker("SOLEUR", "150.00", bid="149.90", ask="150.10"),
    ticker("BNBEUR", "600.00", bid="600.00", ask="600.10"),
    # ADAEUR has no current price.
)


@pytest.fixture
def fake_binance():
    return FakeBinance()


@pytest.fixture
async def market_service():
    return await make_market_service(*DEFAULT_TICKERS)


@pytest.fixture
def history_service(fake_binance):
    rest_client = BinanceRestClient(SETTINGS, transport=httpx.MockTransport(fake_binance))
    return HistoryService(rest_client, clock=lambda: NOW)


@pytest.fixture
def client(market_service, history_service):
    app.dependency_overrides[get_market_service] = lambda: market_service
    app.dependency_overrides[get_history_service] = lambda: history_service
    # No `with` block: the lifespan (and the real Binance client) does not start.
    yield TestClient(app)
    app.dependency_overrides.clear()


def get_value(client, **params):
    response = client.get("/api/history/value", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def by_symbol(body):
    return {item["symbol"]: item for item in body["results"]}


@pytest.mark.parametrize(
    ("period", "expected_start"),
    [
        ("24h", datetime(2026, 10, 17, 12, tzinfo=UTC)),
        ("7d", datetime(2026, 10, 11, 12, tzinfo=UTC)),
        ("30d", datetime(2026, 9, 18, 12, tzinfo=UTC)),
        ("90d", datetime(2026, 7, 20, 12, tzinfo=UTC)),
        ("1y", datetime(2025, 10, 18, 12, tzinfo=UTC)),
    ],
)
def test_period_uses_the_hour_candle_containing_the_moment(
    client, fake_binance, period, expected_start
):
    body = get_value(client, amount="100", period=period, symbol="BTCEUR")

    assert fake_binance.start_times() == [expected_start]
    assert body["period"] == period
    assert datetime.fromisoformat(body["requested_at"]) == expected_start.replace(
        minute=30, second=15
    )
    assert datetime.fromisoformat(body["candle_open_time"]) == expected_start
    result = body["results"][0]["result"]
    assert datetime.fromisoformat(result["candle_open_time"]) == expected_start


@pytest.mark.parametrize(
    ("at", "expected_start"),
    [
        ("2026-09-18T12:45:30Z", datetime(2026, 9, 18, 12, tzinfo=UTC)),
        ("2026-09-18T12:00:00Z", datetime(2026, 9, 18, 12, tzinfo=UTC)),
        ("2026-09-18T12:59:59.999Z", datetime(2026, 9, 18, 12, tzinfo=UTC)),
        # 14:10 in Belgrade (UTC+2) is 12:10 UTC.
        ("2026-09-18T14:10:00+02:00", datetime(2026, 9, 18, 12, tzinfo=UTC)),
    ],
)
def test_at_uses_the_hour_candle_containing_the_moment(client, fake_binance, at, expected_start):
    body = get_value(client, amount="100", at=at, symbol="BTCEUR")

    assert fake_binance.start_times() == [expected_start]
    assert body["period"] is None
    assert datetime.fromisoformat(body["candle_open_time"]) == expected_start


def test_gain_is_computed_from_open_price_and_current_bid(client):
    results = by_symbol(get_value(client, amount="100", period="30d"))

    btc = results["BTCEUR"]["result"]
    assert btc["historical_price"] == "50000"
    # 100 / 50000
    assert btc["quantity"] == "0.00200000"
    assert btc["current_price"] == "72416.85"
    assert btc["current_price_type"] == "bid"
    assert btc["freshness"] == "live"
    assert btc["current_price_updated_at"] is not None
    # 0.002 * 72416.85 = 144.8337
    assert btc["value_eur"] == "144.83"
    assert btc["profit_eur"] == "44.83"
    assert btc["profit_percent"] == "44.83"
    assert results["BTCEUR"]["reason"] is None


def test_loss_is_negative(client):
    sol = by_symbol(get_value(client, amount="100", period="30d"))["SOLEUR"]["result"]

    # 100 / 200 = 0.5; 0.5 * 149.90 = 74.95
    assert sol["quantity"] == "0.50000000"
    assert sol["value_eur"] == "74.95"
    assert sol["profit_eur"] == "-25.05"
    assert sol["profit_percent"] == "-25.05"


def test_current_price_falls_back_to_last_without_bid(client):
    eth = by_symbol(get_value(client, amount="100", period="30d"))["ETHEUR"]["result"]

    assert eth["current_price_type"] == "last"
    assert eth["current_price"] == "2143.10"
    # 100 / 2000 = 0.05; 0.05 * 2143.10 = 107.155
    assert eth["value_eur"] == "107.16"
    assert eth["profit_eur"] == "7.16"
    assert eth["profit_percent"] == "7.16"


async def test_quantity_rounds_down_and_eur_rounds_half_up(history_service, fake_binance):
    fake_binance.open_prices["BTCEUR"] = "3"
    fake_binance.open_prices["ETHEUR"] = "10"
    service = await make_market_service(
        ticker("BTCEUR", "4.5", bid="4.5"), ticker("ETHEUR", "10.025", bid="10.025")
    )
    markets = service.snapshot()["markets"][:2]

    response = await history_service.value_over_time(
        Decimal("2"), NOW - timedelta(days=1), "24h", markets
    )
    btc, eth = (item.result for item in response.results)

    # 2 / 3 = 0.666666666...; ROUND_HALF_UP would give 0.66666667.
    assert str(btc.quantity) == "0.66666666"
    # 0.66666666 * 4.5 = 2.99999997 -> 3.00; profit 0.99999997 -> 1.00; 49.9999985% -> 50.00
    assert str(btc.value_eur) == "3.00"
    assert str(btc.profit_eur) == "1.00"
    assert str(btc.profit_percent) == "50.00"
    # 0.2 * 10.025 = 2.005 and profit 0.005: ROUND_HALF_EVEN would give 2.00 and 0.00.
    assert str(eth.quantity) == "0.20000000"
    assert str(eth.value_eur) == "2.01"
    assert str(eth.profit_eur) == "0.01"
    assert str(eth.profit_percent) == "0.25"


async def test_tiny_loss_is_not_shown_as_negative_zero(history_service, fake_binance):
    fake_binance.open_prices["BTCEUR"] = "3"
    service = await make_market_service(ticker("BTCEUR", "3", bid="3"))

    response = await history_service.value_over_time(
        Decimal("1"),
        NOW - timedelta(days=1),
        "24h",
        service.snapshot()["markets"][:1],
    )
    result = response.results[0].result

    # 1 / 3 rounded down is 0.33333333, worth 0.99999999: a loss of 0.00000001 EUR.
    assert str(result.profit_eur) == "0.00"
    assert str(result.profit_percent) == "0.00"


def test_pair_without_candle_for_that_date_is_null_with_reason(client):
    # 30 days back is before BNBEUR was listed; Binance answers with its first candle.
    bnb = by_symbol(get_value(client, amount="100", period="30d"))["BNBEUR"]

    assert bnb["result"] is None
    assert bnb["reason"] == "nema podataka za taj datum"
    assert bnb["display_symbol"] == "BNB/EUR"


def test_pair_after_listing_has_a_result(client):
    bnb = by_symbol(get_value(client, amount="100", period="7d"))["BNBEUR"]

    assert bnb["reason"] is None
    # 100 / 500 = 0.2; 0.2 * 600 = 120
    assert bnb["result"]["value_eur"] == "120.00"


def test_empty_binance_answer_is_null_with_reason(client, fake_binance):
    del fake_binance.open_prices["ETHEUR"]

    eth = by_symbol(get_value(client, amount="100", period="7d"))["ETHEUR"]

    assert eth["result"] is None
    assert eth["reason"] == "nema podataka za taj datum"


def test_pair_without_current_price_is_null_with_reason(client):
    ada = by_symbol(get_value(client, amount="100", period="7d"))["ADAEUR"]

    assert ada["result"] is None
    assert ada["reason"] == "nema trenutne cene"


def test_all_pairs_are_returned_without_symbol(client):
    body = get_value(client, amount="100", period="7d")

    assert [item["symbol"] for item in body["results"]] == SETTINGS.supported_symbols
    assert body["amount"] == "100"


def test_symbol_filter_accepts_display_form(client, fake_binance):
    body = get_value(client, amount="100", period="7d", symbol="sol/eur")

    assert [item["symbol"] for item in body["results"]] == ["SOLEUR"]
    assert len(fake_binance.requests) == 1


def test_unknown_symbol_returns_404(client):
    response = client.get("/api/history/value?amount=100&period=7d&symbol=DOGEEUR")

    assert response.status_code == 404


@pytest.mark.parametrize(
    "query",
    [
        # Neither and both.
        "amount=100",
        "amount=100&period=7d&at=2026-09-18T12:00:00Z",
        # In the future, now, and more than a year back.
        "amount=100&at=2026-10-18T13:00:00Z",
        "amount=100&at=2026-10-18T12:30:15Z",
        "amount=100&at=2025-10-18T12:30:14Z",
        # No time zone, not a date, unknown period.
        "amount=100&at=2026-09-18T12:00:00",
        "amount=100&at=yesterday",
        "amount=100&period=2y",
        # Amount rules from /api/convert.
        "amount=0&period=7d",
        "amount=-1&period=7d",
        "amount=1.123456789&period=7d",
        "amount=1000000000.01&period=7d",
        "amount=NaN&period=7d",
        "period=7d",
    ],
)
def test_invalid_request_returns_422(client, fake_binance, query):
    response = client.get(f"/api/history/value?{query}")

    assert response.status_code == 422, response.text
    assert fake_binance.requests == []


def test_exactly_one_year_back_is_allowed(client):
    assert client.get("/api/history/value?amount=100&at=2025-10-18T12:30:15Z").status_code == 200


def test_repeated_request_is_served_from_cache(client, fake_binance):
    first = get_value(client, amount="100", period="30d")
    calls = len(fake_binance.requests)

    second = get_value(client, amount="250", period="30d")

    assert calls == len(SETTINGS.supported_symbols)
    assert len(fake_binance.requests) == calls
    assert first["candle_open_time"] == second["candle_open_time"]
    # Missing candles are cached too: BNBEUR stays null without a new request.
    assert by_symbol(second)["BNBEUR"]["result"] is None


async def test_cache_keeps_at_most_max_entries(fake_binance):
    rest_client = BinanceRestClient(SETTINGS, transport=httpx.MockTransport(fake_binance))
    history = HistoryService(rest_client, clock=lambda: NOW, max_cache_entries=2)
    service = await make_market_service(*DEFAULT_TICKERS)
    btc = service.snapshot()["markets"][:1]

    for days in (1, 2, 3):
        await history.value_over_time(Decimal("1"), NOW - timedelta(days=days), "24h", btc)
    # The oldest hour was evicted, the newest two are still cached.
    await history.value_over_time(Decimal("1"), NOW - timedelta(days=3), "24h", btc)
    assert len(fake_binance.requests) == 3
    await history.value_over_time(Decimal("1"), NOW - timedelta(days=1), "24h", btc)
    assert len(fake_binance.requests) == 4
    await rest_client.close()


def test_binance_unavailable_returns_503(market_service):
    def fail(request):
        raise httpx.ConnectError("no network")

    rest_client = BinanceRestClient(SETTINGS, transport=httpx.MockTransport(fail))
    history = HistoryService(rest_client, clock=lambda: NOW)
    app.dependency_overrides[get_market_service] = lambda: market_service
    app.dependency_overrides[get_history_service] = lambda: history
    try:
        response = TestClient(app).get("/api/history/value?amount=100&period=7d")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json() == {"detail": "Berza trenutno nije dostupna"}


def test_rate_limit_returns_429(client):
    for _ in range(20):
        assert client.get("/api/history/value?amount=100&period=7d").status_code == 200

    response = client.get("/api/history/value?amount=100&period=7d")

    assert response.status_code == 429
    assert "Retry-After" in response.headers
