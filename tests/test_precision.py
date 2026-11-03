import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_precision_service
from app.config import Settings
from app.main import app
from app.services.exchange_client import (
    BinanceRestClient,
    ExchangeUnavailableError,
    SymbolPrecision,
    parse_exchange_info,
    step_decimals,
)
from app.services.precision_service import PrecisionService

SETTINGS = Settings(_env_file=None)
SYMBOLS = ["BTCEUR", "ADAEUR"]


def symbol_info(symbol: str, tick_size: str | None, step_size: str | None) -> dict:
    """One /api/v3/exchangeInfo symbol entry, trimmed to the fields that matter here."""
    filters: list[dict] = []
    if tick_size is not None:
        filters.append(
            {
                "filterType": "PRICE_FILTER",
                "minPrice": tick_size,
                "maxPrice": "1000000.00000000",
                "tickSize": tick_size,
            }
        )
    if step_size is not None:
        filters.append(
            {
                "filterType": "LOT_SIZE",
                "minQty": step_size,
                "maxQty": "9000.00000000",
                "stepSize": step_size,
            }
        )
    return {"symbol": symbol, "status": "TRADING", "filters": filters}


EXCHANGE_INFO = {
    "timezone": "UTC",
    "serverTime": 1792591500000,
    "rateLimits": [],
    "exchangeFilters": [],
    "symbols": [
        symbol_info("BTCEUR", "0.01000000", "0.00001000"),
        symbol_info("ADAEUR", "0.00010000", "0.10000000"),
    ],
}


class FakeBinance:
    """httpx.MockTransport handler for /api/v3/exchangeInfo; answers with the queued statuses."""

    def __init__(self, statuses: list[int], payload: dict = EXCHANGE_INFO) -> None:
        self.statuses = statuses
        self.payload = payload
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status = self.statuses.pop(0) if self.statuses else 200
        if status != 200:
            return httpx.Response(status, text="error")
        return httpx.Response(200, json=self.payload)


@pytest.mark.parametrize(
    ("step", "decimals"),
    [
        ("0.01000000", 2),
        ("0.00010000", 4),
        ("0.00001000", 5),
        ("0.00000001", 8),
        ("0.10000000", 1),
        ("0.50000000", 1),
        ("1.00000000", 0),
        ("10.00000000", 0),
    ],
)
def test_step_decimals(step, decimals):
    assert step_decimals(step) == decimals


@pytest.mark.parametrize("step", ["0.00000000", "-0.01", "abc", "NaN", "", None, 0.01])
def test_step_decimals_is_none_for_unusable_steps(step):
    # "0" disables the rule on Binance; floats are refused like everywhere else.
    assert step_decimals(step) is None


def test_missing_filter_gives_null():
    data = {
        "symbols": [
            symbol_info("BTCEUR", "0.01000000", None),
            symbol_info("ADAEUR", None, "0.10000000"),
        ]
    }

    assert parse_exchange_info(data, set(SYMBOLS)) == {
        "BTCEUR": SymbolPrecision(price_decimals=2, quantity_decimals=None),
        "ADAEUR": SymbolPrecision(price_decimals=None, quantity_decimals=1),
    }


def test_unsupported_symbols_are_ignored():
    data = {"symbols": [symbol_info("BTCUSDT", "0.01000000", "0.00001000")]}

    assert parse_exchange_info(data, set(SYMBOLS)) == {}


def test_unexpected_payload_is_an_exchange_error():
    with pytest.raises(ExchangeUnavailableError):
        parse_exchange_info({"code": -1121, "msg": "Invalid symbol."}, set(SYMBOLS))


async def test_client_requests_all_symbols_at_once():
    fake = FakeBinance([200])
    client = BinanceRestClient(SETTINGS, transport=httpx.MockTransport(fake))

    precision = await client.get_exchange_info(SYMBOLS)

    assert precision == {
        "BTCEUR": SymbolPrecision(price_decimals=2, quantity_decimals=5),
        "ADAEUR": SymbolPrecision(price_decimals=4, quantity_decimals=1),
    }
    [request] = fake.requests
    assert request.url.path == "/api/v3/exchangeInfo"
    assert json.loads(request.url.params["symbols"]) == SYMBOLS
    await client.close()


def make_service(fake: FakeBinance) -> tuple[PrecisionService, list[float]]:
    client = BinanceRestClient(SETTINGS, transport=httpx.MockTransport(fake))
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    service = PrecisionService(lambda: client.get_exchange_info(SYMBOLS), SYMBOLS, sleep=fake_sleep)
    return service, sleeps


async def test_failure_then_successful_retry(caplog):
    fake = FakeBinance([503])
    service, sleeps = make_service(fake)

    with caplog.at_level(logging.WARNING):
        await service.run()

    assert len(fake.requests) == 2
    assert sleeps == [60.0]
    assert "retry 1/5 in 60 s" in caplog.text
    assert [meta.model_dump() for meta in service.view()] == [
        {"symbol": "BTCEUR", "price_decimals": 2, "quantity_decimals": 5},
        {"symbol": "ADAEUR", "price_decimals": 4, "quantity_decimals": 1},
    ]


async def test_gives_up_after_five_retries_and_stays_null(caplog):
    fake = FakeBinance([500] * 10)
    service, sleeps = make_service(fake)

    with caplog.at_level(logging.WARNING):
        await service.run()

    assert len(fake.requests) == 6
    assert sleeps == [60.0] * 5
    assert "giving up" in caplog.text
    assert all(
        meta.price_decimals is None and meta.quantity_decimals is None for meta in service.view()
    )


async def test_pair_missing_from_the_answer_stays_null(caplog):
    fake = FakeBinance([200], payload={"symbols": [symbol_info("BTCEUR", "0.01", "0.00001")]})
    service, _ = make_service(fake)

    with caplog.at_level(logging.WARNING):
        await service.run()

    assert "no entry for ADAEUR" in caplog.text
    assert service.view()[1].model_dump() == {
        "symbol": "ADAEUR",
        "price_decimals": None,
        "quantity_decimals": None,
    }


@pytest.fixture
def api_client():
    service, _ = make_service(FakeBinance([200]))
    app.dependency_overrides[get_precision_service] = lambda: service
    # No `with` block: the lifespan (and the real Binance client) does not start.
    yield TestClient(app), service
    app.dependency_overrides.clear()


def test_endpoint_is_null_before_load(api_client):
    client, _ = api_client

    response = client.get("/api/markets/meta")

    assert response.status_code == 200
    assert response.json() == [
        {"symbol": "BTCEUR", "price_decimals": None, "quantity_decimals": None},
        {"symbol": "ADAEUR", "price_decimals": None, "quantity_decimals": None},
    ]


async def test_endpoint_returns_decimals_after_load(api_client):
    client, service = api_client
    await service.run()

    response = client.get("/api/markets/meta")

    assert response.status_code == 200
    assert response.json() == [
        {"symbol": "BTCEUR", "price_decimals": 2, "quantity_decimals": 5},
        {"symbol": "ADAEUR", "price_decimals": 4, "quantity_decimals": 1},
    ]
