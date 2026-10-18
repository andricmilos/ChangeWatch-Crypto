from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.dependencies import get_market_service, normalize_symbol
from app.api.routes_market import to_market_view
from app.config import Settings
from app.main import app
from app.services.exchange_client import parse_rolling_ticker, parse_ticker
from app.services.market_data_service import MarketDataService

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
    "b": "72416.80",
    "a": "72416.90",
}


@pytest.fixture
async def fake_service():
    # Never started, so there is no Binance connection; data is written in directly.
    service = MarketDataService(Settings(_env_file=None))
    service.client.status = "connected"
    # Freshness follows the connection: it counts as live while any stream sends messages.
    service.client.last_message_at = datetime.now(UTC)
    await service.handle_ticker(parse_ticker(BTC_TICKER))
    await service.handle_rolling(parse_rolling_ticker({**BTC_TICKER, "e": "1hTicker", "P": "0.35"}))
    return service


@pytest.fixture
def client(fake_service):
    app.dependency_overrides[get_market_service] = lambda: fake_service
    # No `with` block: the lifespan (and the real Binance client) does not start.
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_list_markets_returns_all_pairs(client):
    response = client.get("/api/markets")

    assert response.status_code == 200
    body = response.json()
    assert body["connection_status"] == "connected"
    assert [m["symbol"] for m in body["markets"]] == [
        "BTCEUR",
        "ETHEUR",
        "SOLEUR",
        "BNBEUR",
        "ADAEUR",
    ]


def test_prices_are_strings_and_fresh(client):
    btc = client.get("/api/markets").json()["markets"][0]

    assert btc["display_symbol"] == "BTC/EUR"
    assert btc["last_price"] == "72416.85"
    assert btc["price_change"] == "-440.10"
    assert btc["price_change_percent"] == "-0.60"
    assert btc["high_24h"] == "73350.00"
    assert btc["low_24h"] == "71820.10"
    assert btc["volume_24h"] == "412.551"
    assert btc["bid_price"] == "72416.80"
    assert btc["ask_price"] == "72416.90"
    assert btc["spread"] == "0.10"
    assert btc["spread_percent"] == "0.000"
    assert btc["high_spread"] is False
    assert btc["updated_at"] is not None
    assert btc["freshness"] == "live"


def test_markets_include_rolling_changes(client):
    btc = client.get("/api/markets").json()["markets"][0]

    assert btc["change_1h_percent"] == "0.35"
    # The 4h stream has not sent anything for BTC yet.
    assert btc["change_4h_percent"] is None


def test_get_single_market(client):
    response = client.get("/api/markets/BTCEUR")

    assert response.status_code == 200
    assert Decimal(response.json()["last_price"]) == Decimal("72416.85")


@pytest.mark.parametrize("raw", ["btceur", "BTC-EUR"])
def test_symbol_variants_are_normalized(client, raw):
    response = client.get(f"/api/markets/{raw}")

    assert response.status_code == 200
    assert response.json()["symbol"] == "BTCEUR"


def test_unknown_symbol_returns_404(client):
    assert client.get("/api/markets/DOGEEUR").status_code == 404


def test_market_without_data_has_null_price(client):
    eth = client.get("/api/markets/ETHEUR").json()

    assert eth["last_price"] is None
    assert eth["volume_24h"] is None
    assert eth["bid_price"] is None
    assert eth["ask_price"] is None
    assert eth["spread"] is None
    assert eth["spread_percent"] is None
    assert eth["high_spread"] is None
    assert eth["change_1h_percent"] is None
    assert eth["change_4h_percent"] is None
    assert eth["updated_at"] is None
    assert eth["freshness"] == "no_data"


@pytest.mark.parametrize("raw", ["btceur", "BTC-EUR", "BTC/EUR", " BTCEUR "])
def test_normalize_symbol_accepts_common_formats(raw):
    assert normalize_symbol(raw, {"BTCEUR"}) == "BTCEUR"


def test_normalize_symbol_rejects_unknown():
    with pytest.raises(HTTPException) as exc_info:
        normalize_symbol("DOGEEUR", {"BTCEUR"})
    assert exc_info.value.status_code == 404


def market_with_book(bid, ask):
    return {
        "symbol": "BTCEUR",
        "display_symbol": "BTC/EUR",
        "ticker": parse_ticker({**BTC_TICKER, "b": bid, "a": ask}),
        "change_1h_percent": None,
        "change_4h_percent": None,
        "updated_at": None,
        "freshness": "live",
    }


@pytest.mark.parametrize(
    ("bid", "ask", "spread_percent", "high"),
    [
        # Exactly at the 0.5 % threshold is not "above" it.
        ("99.75", "100.25", "0.500", False),
        ("99.7495", "100.2505", "0.501", True),
        ("99.7505", "100.2495", "0.499", False),
    ],
)
def test_high_spread_at_threshold(bid, ask, spread_percent, high):
    view = to_market_view(market_with_book(bid, ask), Decimal("0.5"))

    assert view.spread_percent == Decimal(spread_percent)
    assert view.high_spread is high


def test_high_spread_is_null_without_bid_ask():
    view = to_market_view(market_with_book(None, None), Decimal("0.5"))

    assert view.last_price == Decimal("72416.85")
    assert view.spread_percent is None
    assert view.high_spread is None


def test_spread_warning_threshold_comes_from_settings(fake_service):
    fake_service.settings = Settings(_env_file=None, spread_warning_percent="0.0001")
    app.dependency_overrides[get_market_service] = lambda: fake_service
    try:
        btc = TestClient(app).get("/api/markets/BTCEUR").json()
    finally:
        app.dependency_overrides.clear()

    # 0.000 % is still not above 0.0001 %.
    assert btc["high_spread"] is False
