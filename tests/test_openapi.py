import tomllib
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.dependencies import (
    get_alert_service,
    get_candle_service,
    get_connection_manager,
    get_history_service,
    get_market_service,
    get_runtime_stats,
)
from app.api.rate_limit import RateLimiter, export_limiter, history_limiter, write_limiter
from app.config import Settings
from app.database import Base, get_session
from app.main import app
from app.schemas.status import StatusResponse
from app.services.exchange_client import ExchangeUnavailableError
from app.services.history_service import HistoryService
from app.services.market_data_service import MarketDataService
from app.services.runtime_stats import RuntimeStats
from app.websocket.manager import ConnectionManager

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
# Which Python type `detail` has for each documented error model.
DETAIL_TYPES = {"ErrorResponse": str, "ValidationErrorResponse": list}


class UnavailableCandles:
    async def get_candles(self, symbol: str, interval: str, limit: int) -> list[Any]:
        raise ExchangeUnavailableError("Binance REST request failed")


class UnavailableHistory(HistoryService):
    """Real moment checks, but every Binance lookup fails."""

    def __init__(self) -> None:
        super().__init__(rest_client=None)  # type: ignore[arg-type]  # never called

    async def value_over_time(self, *args: Any, **kwargs: Any) -> Any:
        raise ExchangeUnavailableError("Binance REST request failed")


class FullAlertService:
    """Stands in for AlertService: already at the active-alert limit, and no alert exists."""

    def active_count(self) -> int:
        return 50

    async def update(self, alert_id: int, payload: Any) -> None:
        return None

    async def delete(self, alert_id: int) -> bool:
        return False


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    # In-memory SQLite, so tests never touch data/changewatch.db.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def client(session_factory):
    stats = RuntimeStats()
    market_service = MarketDataService(Settings(_env_file=None), stats)  # never started
    manager = ConnectionManager(stats)

    async def test_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    original_overrides = dict(app.dependency_overrides)
    app.dependency_overrides.update(
        {
            get_market_service: lambda: market_service,
            get_session: test_session,
            get_candle_service: UnavailableCandles,
            get_history_service: UnavailableHistory,
            get_alert_service: FullAlertService,
            get_runtime_stats: lambda: stats,
            get_connection_manager: lambda: manager,
        }
    )
    # No `with` block: the lifespan (real Binance client, real database) does not start.
    yield TestClient(app)
    app.dependency_overrides.clear()
    app.dependency_overrides.update(original_overrides)


@pytest.fixture
def schema(client) -> dict[str, Any]:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    return response.json()


def api_operations(schema: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (method, path, operation)
        for path, item in schema["paths"].items()
        if path.startswith("/api/")
        for method, operation in item.items()
        if method in HTTP_METHODS
    ]


def documented_detail_types(response: dict[str, Any]) -> set[type]:
    schema = response["content"]["application/json"]["schema"]
    refs = [option["$ref"] for option in schema.get("anyOf", [schema])]
    return {DETAIL_TYPES[ref.rsplit("/", 1)[-1]] for ref in refs}


def assert_errors_match(
    schema: dict[str, Any],
    method: str,
    path: str,
    cases: list[tuple[int, Callable[[], Response]]],
) -> None:
    """Every documented error status is produced, and with the documented body shape."""
    responses = schema["paths"][path][method]["responses"]
    documented = {int(code) for code in responses if int(code) >= 400}
    assert documented == {status for status, _ in cases}

    for status, send in cases:
        response = send()
        assert response.status_code == status, (path, status, response.text)
        documented_response = responses[str(status)]
        assert type(response.json()["detail"]) in documented_detail_types(documented_response)
        if "Retry-After" in documented_response.get("headers", {}):
            assert int(response.headers["Retry-After"]) >= 1


def over_limit(limiter: RateLimiter, send: Callable[[], Response]) -> Callable[[], Response]:
    """Sends twice with a budget of one request, so the second answer is the 429."""

    def run() -> Response:
        original = limiter.max_requests
        limiter.reset()
        limiter.max_requests = 1
        try:
            send()
            return send()
        finally:
            limiter.max_requests = original
            limiter.reset()

    return run


def test_openapi_has_app_metadata(schema):
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())

    assert schema["info"]["title"] == "ChangeWatch Crypto"
    assert schema["info"]["version"] == pyproject["project"]["version"]
    assert "does not place orders" in schema["info"]["description"]


def test_every_public_endpoint_has_summary_description_and_described_tag(schema):
    described_tags = {tag["name"] for tag in schema["tags"] if tag.get("description")}
    operations = api_operations(schema)

    assert operations
    for method, path, operation in operations:
        assert operation.get("summary"), (method, path)
        assert operation.get("description"), (method, path)
        assert operation.get("tags"), (method, path)
        assert set(operation["tags"]) <= described_tags, (method, path)


def test_internal_routes_are_hidden_from_schema(schema):
    assert "/partials/alerts" not in schema["paths"]
    assert "/health" not in schema["paths"]
    assert "/" not in schema["paths"]


def test_error_model_is_shared(schema):
    error = schema["components"]["schemas"]["ErrorResponse"]

    assert error["required"] == ["detail"]
    assert error["properties"]["detail"]["type"] == "string"


def test_market_candles_errors_match_documentation(client, schema):
    url = "/api/markets/{symbol}/candles"
    assert_errors_match(
        schema,
        "get",
        url,
        [
            (404, lambda: client.get("/api/markets/XRPEUR/candles")),
            (422, lambda: client.get("/api/markets/BTCEUR/candles", params={"interval": "2m"})),
            (422, lambda: client.get("/api/markets/BTCEUR/candles", params={"limit": 0})),
            (503, lambda: client.get("/api/markets/BTCEUR/candles")),
        ],
    )


def test_convert_errors_match_documentation(client, schema):
    assert_errors_match(
        schema,
        "get",
        "/api/convert",
        [
            (422, lambda: client.get("/api/convert", params={"amount": 10, "currency": "XYZ"})),
            (422, lambda: client.get("/api/convert", params={"amount": 0})),
        ],
    )


def test_history_errors_match_documentation(client, schema):
    def value(**params: Any) -> Callable[[], Response]:
        return lambda: client.get("/api/history/value", params={"amount": 100, **params})

    assert_errors_match(
        schema,
        "get",
        "/api/history/value",
        [
            (404, value(period="24h", symbol="XRPEUR")),
            (422, value()),
            (422, value(period="2y")),
            (503, value(period="24h")),
            (429, over_limit(history_limiter, value(period="24h"))),
        ],
    )


def test_watchlist_errors_match_documentation(client, schema):
    assert client.post("/api/watchlist", json={"symbol": "BTCEUR"}).status_code == 201
    assert_errors_match(
        schema,
        "post",
        "/api/watchlist",
        [
            (409, lambda: client.post("/api/watchlist", json={"symbol": "BTCEUR"})),
            (422, lambda: client.post("/api/watchlist", json={"symbol": "XRPEUR"})),
            (429, over_limit(write_limiter, lambda: client.delete("/api/watchlist/ETHEUR"))),
        ],
    )


def test_alerts_errors_match_documentation(client, schema):
    body = {"symbol": "BTCEUR", "condition": "above", "target_price": "75000"}
    assert_errors_match(
        schema,
        "post",
        "/api/alerts",
        [
            (400, lambda: client.post("/api/alerts", json=body)),
            (422, lambda: client.post("/api/alerts", json={**body, "target_price": "0"})),
            (429, over_limit(write_limiter, lambda: client.post("/api/alerts", json=body))),
        ],
    )
    assert_errors_match(
        schema,
        "patch",
        "/api/alerts/{alert_id}",
        [
            (404, lambda: client.patch("/api/alerts/999", json={"is_active": False})),
            (422, lambda: client.patch("/api/alerts/abc", json={"is_active": False})),
            (429, over_limit(write_limiter, lambda: client.patch("/api/alerts/999", json={}))),
        ],
    )


def test_export_errors_match_documentation(client, schema):
    def candles(**params: Any) -> Callable[[], Response]:
        query = {"symbol": "BTCEUR", "from_ms": 0, "to_ms": 60_000, **params}
        return lambda: client.get("/api/export/candles.csv", params=query)

    assert_errors_match(
        schema,
        "get",
        "/api/export/candles.csv",
        [
            (422, candles(symbol="XRPEUR")),
            (422, candles(interval="5m")),
            (429, over_limit(export_limiter, candles())),
        ],
    )
    assert_errors_match(
        schema,
        "get",
        "/api/export/alerts.csv",
        [(429, over_limit(export_limiter, lambda: client.get("/api/export/alerts.csv")))],
    )


def test_status_documents_its_response_shape(client, schema):
    operation = schema["paths"]["/api/status"]["get"]
    response = client.get("/api/status")

    assert {int(code) for code in operation["responses"]} == {200}
    assert response.status_code == 200
    StatusResponse.model_validate(response.json())
