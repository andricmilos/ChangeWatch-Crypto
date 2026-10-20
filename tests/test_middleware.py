import logging
import re

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.api.dependencies import get_market_service
from app.config import Settings
from app.main import app
from app.middleware import RequestTimingMiddleware
from app.services.market_data_service import MarketDataService

MIDDLEWARE_LOGGER = "app.middleware"


def middleware_records(caplog) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == MIDDLEWARE_LOGGER]


@pytest.fixture
def client(caplog):
    caplog.set_level(logging.INFO, logger=MIDDLEWARE_LOGGER)
    # Never started, so there is no Binance connection.
    service = MarketDataService(Settings(_env_file=None))
    original_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_market_service] = lambda: service
    # No `with` block: the lifespan (and the real Binance client) does not start.
    yield TestClient(app)
    app.dependency_overrides.clear()
    app.dependency_overrides.update(original_overrides)


def fake_clock(step_seconds: float):
    """Each call returns a time `step_seconds` later than the previous one."""
    now = 0.0

    def clock() -> float:
        nonlocal now
        current = now
        now += step_seconds
        return current

    return clock


def make_test_app(clock=None) -> FastAPI:
    test_app = FastAPI()
    if clock is None:
        test_app.add_middleware(RequestTimingMiddleware)
    else:
        test_app.add_middleware(RequestTimingMiddleware, clock=clock)

    @test_app.get("/api/ping")
    async def ping() -> dict[str, str]:
        return {"status": "ok"}

    @test_app.get("/api/boom")
    async def boom() -> None:
        raise RuntimeError("boom")

    @test_app.exception_handler(Exception)
    async def error_handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=500, content={"detail": "Interna greška"})

    return test_app


def test_response_has_server_timing_header(client):
    response = client.get("/api/markets")

    assert response.status_code == 200
    assert re.fullmatch(r"app;dur=\d+(\.\d+)?", response.headers["Server-Timing"])


def test_api_request_logs_one_line(client, caplog):
    client.get("/api/markets")

    records = middleware_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    assert re.fullmatch(r"GET /api/markets 200 \d+\.\dms", records[0].getMessage())


def test_health_and_static_are_not_logged(client, caplog):
    health = client.get("/health")
    static = client.get("/static/css/style.css")

    assert health.status_code == 200
    assert static.status_code == 200
    assert "Server-Timing" in health.headers
    assert middleware_records(caplog) == []


def test_query_string_is_not_logged(client, caplog):
    client.get("/api/markets?secret=hunter2")

    message = middleware_records(caplog)[0].getMessage()
    assert message.startswith("GET /api/markets 200 ")
    assert "secret" not in message
    assert "?" not in message


def test_slow_request_logs_warning(caplog):
    caplog.set_level(logging.INFO, logger=MIDDLEWARE_LOGGER)
    client = TestClient(make_test_app(clock=fake_clock(step_seconds=0.6)))

    response = client.get("/api/ping")

    assert response.status_code == 200
    records = middleware_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].getMessage().startswith("GET /api/ping 200 ")


def test_route_exception_logs_500_and_reaches_error_handler(caplog):
    caplog.set_level(logging.INFO, logger=MIDDLEWARE_LOGGER)
    client = TestClient(make_test_app(), raise_server_exceptions=False)

    response = client.get("/api/boom")

    assert response.status_code == 500
    assert response.json() == {"detail": "Interna greška"}
    records = middleware_records(caplog)
    assert len(records) == 1
    assert re.fullmatch(r"GET /api/boom 500 \d+\.\dms", records[0].getMessage())
