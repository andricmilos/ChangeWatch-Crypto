from decimal import Decimal

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.dependencies import get_market_service, normalize_symbol
from app.config import Settings
from app.main import app
from app.services.exchange_client import parse_ticker
from app.services.market_data_service import MarketDataService

BTC_TICKER = {
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


@pytest.fixture
async def fake_service():
    # Never started, so there is no Binance connection; data is written in directly.
    service = MarketDataService(Settings(_env_file=None))
    service.client.status = "connected"
    await service.handle_ticker(parse_ticker(BTC_TICKER))
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
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "BNBUSDT",
        "ADAUSDT",
    ]


def test_prices_are_strings_and_fresh(client):
    btc = client.get("/api/markets").json()["markets"][0]

    assert btc["display_symbol"] == "BTC/USDT"
    assert btc["last_price"] == "113250.55"
    assert btc["price_change"] == "-512.10"
    assert btc["price_change_percent"] == "-0.45"
    assert btc["high_24h"] == "114900.00"
    assert btc["low_24h"] == "112600.10"
    assert btc["volume_24h"] == "18234.551"
    assert btc["updated_at"] is not None
    assert btc["freshness"] == "live"


def test_get_single_market(client):
    response = client.get("/api/markets/BTCUSDT")

    assert response.status_code == 200
    assert Decimal(response.json()["last_price"]) == Decimal("113250.55")


@pytest.mark.parametrize("raw", ["btcusdt", "BTC-USDT"])
def test_symbol_variants_are_normalized(client, raw):
    response = client.get(f"/api/markets/{raw}")

    assert response.status_code == 200
    assert response.json()["symbol"] == "BTCUSDT"


def test_unknown_symbol_returns_404(client):
    assert client.get("/api/markets/DOGEUSDT").status_code == 404


def test_market_without_data_has_null_price(client):
    eth = client.get("/api/markets/ETHUSDT").json()

    assert eth["last_price"] is None
    assert eth["volume_24h"] is None
    assert eth["updated_at"] is None
    assert eth["freshness"] == "no_data"


@pytest.mark.parametrize("raw", ["btcusdt", "BTC-USDT", "BTC/USDT", " BTCUSDT "])
def test_normalize_symbol_accepts_common_formats(raw):
    assert normalize_symbol(raw, {"BTCUSDT"}) == "BTCUSDT"


def test_normalize_symbol_rejects_unknown():
    with pytest.raises(HTTPException) as exc_info:
        normalize_symbol("DOGEUSDT", {"BTCUSDT"})
    assert exc_info.value.status_code == 404
