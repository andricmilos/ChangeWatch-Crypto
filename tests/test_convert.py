from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_market_service
from app.config import Settings
from app.main import app
from app.services.exchange_client import parse_ticker
from app.services.market_data_service import MarketDataService


def ticker(symbol, last, bid=None, ask=None):
    data = {"e": "24hrTicker", "E": 1760380800000, "s": symbol, "c": last}
    if bid is not None:
        data["b"] = bid
    if ask is not None:
        data["a"] = ask
    return parse_ticker(data)


async def make_service(*tickers):
    # Never started, so there is no Binance connection; data is written in directly.
    service = MarketDataService(Settings(_env_file=None))
    service.client.status = "connected"
    service.client.last_message_at = datetime.now(UTC)
    for item in tickers:
        await service.handle_ticker(item)
    return service


@pytest.fixture
async def service():
    return await make_service(
        ticker("BTCEUR", "72416.85", bid="72416.85", ask="72417.00"),
        # No order book in the message: the last price is used both ways.
        ticker("ETHEUR", "2143.10"),
        ticker("SOLEUR", "150.00", bid="149.90", ask="150.10"),
        # Only one side of the book is not enough for bid/ask.
        ticker("ADAEUR", "0.5000", bid="0.4999"),
        # BNBEUR has no data at all.
    )


@pytest.fixture
def client(service):
    app.dependency_overrides[get_market_service] = lambda: service
    # No `with` block: the lifespan (and the real Binance client) does not start.
    yield TestClient(app)
    app.dependency_overrides.clear()


def results_by_currency(response):
    assert response.status_code == 200, response.text
    return {result["currency"]: result for result in response.json()["results"]}


def test_eur_to_crypto_uses_ask_and_rounds_down(client):
    results = results_by_currency(client.get("/api/convert?amount=100&currency=EUR"))

    assert list(results) == ["BTC", "ETH", "SOL", "BNB", "ADA"]
    btc = results["BTC"]
    # 100 / 72417.00 = 0.0013808912...
    assert btc["amount"] == "0.00138089"
    assert btc["symbol"] == "BTCEUR"
    assert btc["price_type"] == "ask"
    assert btc["price"] == "72417.00"
    assert btc["freshness"] == "live"
    assert btc["updated_at"] is not None
    # 100 / 150.10 = 0.6662225183...
    assert results["SOL"]["amount"] == "0.66622251"
    assert results["SOL"]["price_type"] == "ask"


def test_eur_to_crypto_falls_back_to_last_price(client):
    results = results_by_currency(client.get("/api/convert?amount=100&currency=EUR"))

    # 100 / 2143.10 = 0.0466613783...
    assert results["ETH"]["amount"] == "0.04666137"
    assert results["ETH"]["price_type"] == "last"
    assert results["ETH"]["price"] == "2143.10"
    assert results["ADA"]["amount"] == "200.00000000"
    assert results["ADA"]["price_type"] == "last"


async def test_crypto_amount_is_rounded_down_not_half_up():
    service = await make_service(ticker("BTCEUR", "3", bid="3", ask="3"))
    app.dependency_overrides[get_market_service] = lambda: service
    try:
        results = results_by_currency(TestClient(app).get("/api/convert?amount=2&currency=EUR"))
    finally:
        app.dependency_overrides.clear()

    # 2 / 3 = 0.666666666...; ROUND_HALF_UP would give 0.66666667.
    assert results["BTC"]["amount"] == "0.66666666"


def test_crypto_to_eur_uses_bid_and_rounds_half_up(client):
    results = results_by_currency(client.get("/api/convert?amount=0.5&currency=BTC"))

    eur = results["EUR"]
    # 0.5 * 72416.85 = 36208.425; ROUND_HALF_EVEN would give 36208.42.
    assert eur["amount"] == "36208.43"
    assert eur["symbol"] == "BTCEUR"
    assert eur["price_type"] == "bid"
    assert eur["price"] == "72416.85"
    assert eur["freshness"] == "live"


def test_crypto_to_other_crypto_goes_through_eur(client):
    response = client.get("/api/convert?amount=0.5&currency=btc")
    body = response.json()
    results = results_by_currency(response)

    assert body["currency"] == "BTC"
    assert body["amount"] == "0.5"
    # The source currency itself is not listed; EUR comes first.
    assert list(results) == ["EUR", "ETH", "SOL", "BNB", "ADA"]
    # Unrounded EUR value 36208.425 / ask 150.10 = 241.2286808...
    assert results["SOL"]["amount"] == "241.22868087"
    assert results["SOL"]["price_type"] == "ask"
    assert results["SOL"]["price"] == "150.10"
    # 36208.425 / last 2143.10 = 16.8953501...
    assert results["ETH"]["amount"] == "16.89535019"
    assert results["ETH"]["price_type"] == "last"


def test_crypto_without_order_book_sells_at_last_price(client):
    results = results_by_currency(client.get("/api/convert?amount=10&currency=ETH"))

    assert results["EUR"]["amount"] == "21431.00"
    assert results["EUR"]["price_type"] == "last"


def test_pair_without_data_is_null_not_zero(client):
    results = results_by_currency(client.get("/api/convert?amount=100&currency=EUR"))

    bnb = results["BNB"]
    assert bnb["amount"] is None
    assert bnb["price"] is None
    assert bnb["price_type"] is None
    assert bnb["updated_at"] is None
    assert bnb["freshness"] == "no_data"


def test_source_without_data_makes_every_result_null(client):
    results = results_by_currency(client.get("/api/convert?amount=1&currency=BNB"))

    assert all(result["amount"] is None for result in results.values())
    assert all(result["freshness"] == "no_data" for result in results.values())
    assert results["EUR"]["price"] is None
    # The target pair's own price is still shown; only the amount is missing.
    assert results["BTC"]["price"] == "72417.00"


def test_stale_data_is_returned_and_marked(client, service):
    service.client.last_message_at = datetime.now(UTC) - timedelta(minutes=5)

    results = results_by_currency(client.get("/api/convert?amount=100&currency=EUR"))

    assert results["BTC"]["amount"] == "0.00138089"
    assert results["BTC"]["freshness"] == "stale"
    assert results["BNB"]["freshness"] == "no_data"


@pytest.mark.parametrize(
    "amount",
    ["0.00000001", "1000000000", "100.10000000", "1E+3"],
)
def test_amount_limits_are_accepted(client, amount):
    response = client.get("/api/convert", params={"amount": amount, "currency": "EUR"})

    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "amount",
    ["0", "-1", "0.000000001", "1.123456789", "1000000000.01", "abc", "NaN", "Infinity", ""],
)
def test_invalid_amount_returns_422(client, amount):
    response = client.get("/api/convert", params={"amount": amount, "currency": "EUR"})

    assert response.status_code == 422


def test_missing_amount_returns_422(client):
    assert client.get("/api/convert?currency=EUR").status_code == 422


@pytest.mark.parametrize("currency", ["DOGE", "USDT", "BTCEUR", "", "EURO"])
def test_unsupported_currency_returns_422(client, currency):
    response = client.get("/api/convert", params={"amount": "1", "currency": currency})

    assert response.status_code == 422


def test_currency_defaults_to_eur(client):
    response = client.get("/api/convert?amount=1")

    assert response.status_code == 200
    assert response.json()["currency"] == "EUR"


def test_amount_is_kept_exact(client):
    body = client.get("/api/convert?amount=0.10000000&currency=EUR").json()

    assert Decimal(body["amount"]) == Decimal("0.1")
