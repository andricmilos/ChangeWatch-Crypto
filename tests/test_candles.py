from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.dependencies import get_candle_service, get_market_service
from app.config import Settings
from app.database import Base
from app.main import app
from app.models import Candle
from app.schemas.market import KlineUpdate
from app.services.candle_service import CandleService
from app.services.exchange_client import BinanceRestClient, ExchangeUnavailableError
from app.services.market_data_service import MarketDataService

SETTINGS = Settings(_env_file=None)
MINUTE_MS = 60_000
FIRST_OPEN_MS = 1760380800000  # 2025-10-13 18:40:00 UTC


def binance_row(index: int, close: str = "72416.85000000") -> list:
    """One /api/v3/klines row exactly as Binance sends it (numbers as strings, times in ms)."""
    open_time = FIRST_OPEN_MS + index * MINUTE_MS
    return [
        open_time,
        "72300.10000000",
        "72480.00000000",
        "72250.01000000",
        close,
        "18.23455100",
        open_time + MINUTE_MS - 1,
        "1320517.39000000",
        1520,
        "9.10000000",
        "1030000.00000000",
        "0",
    ]


class FakeBinance:
    """httpx.MockTransport handler: answers like Binance, never touches the network."""

    def __init__(self, rows=None, status_code=200, headers=None):
        self.rows = rows if rows is not None else [binance_row(i) for i in range(3)]
        self.status_code = status_code
        self.headers = headers or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status_code != 200:
            return httpx.Response(self.status_code, headers=self.headers, text="error")
        limit = int(request.url.params["limit"])
        return httpx.Response(200, json=self.rows[-limit:])


def make_rest_client(fake: FakeBinance) -> BinanceRestClient:
    return BinanceRestClient(SETTINGS, transport=httpx.MockTransport(fake))


def make_kline(
    interval: str = "1m", is_closed: bool = True, close: str = "72416.85"
) -> KlineUpdate:
    return KlineUpdate(
        symbol="BTCEUR",
        interval=interval,
        open_time=datetime(2025, 10, 13, 18, 40, tzinfo=UTC),
        close_time=datetime(2025, 10, 13, 18, 40, 59, 999000, tzinfo=UTC),
        open=Decimal("72300.10"),
        high=Decimal("72480.00"),
        low=Decimal("72250.01"),
        close=Decimal(close),
        volume=Decimal("18.234551"),
        is_closed=is_closed,
    )


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    # Same in-memory SQLite as the `session` fixture in conftest.py, but CandleService
    # opens its own sessions, so it needs the factory.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def count_candles(session_factory) -> int:
    async with session_factory() as session:
        return await session.scalar(select(func.count()).select_from(Candle))


def make_candle() -> Candle:
    return Candle(
        symbol="BTCEUR",
        interval="1m",
        open_time=1760380800000,
        close_time=1760380859999,
        open_price=Decimal("72300.10000000"),
        high_price=Decimal("72480.00000000"),
        low_price=Decimal("72250.01000000"),
        close_price=Decimal("72416.85000000"),
        volume=Decimal("18.23455100"),
    )


async def test_candle_round_trip_keeps_exact_decimals(session):
    session.add(make_candle())
    await session.commit()
    session.expunge_all()  # force a real read from the database, not the identity map

    candle = (await session.scalars(select(Candle))).one()

    assert isinstance(candle.close_price, Decimal)
    assert candle.close_price == Decimal("72416.85000000")
    # str() also checks trailing zeros survive, i.e. nothing went through float.
    assert str(candle.close_price) == "72416.85000000"
    assert str(candle.open_price) == "72300.10000000"
    assert str(candle.high_price) == "72480.00000000"
    assert str(candle.low_price) == "72250.01000000"
    assert str(candle.volume) == "18.23455100"
    assert candle.open_time == 1760380800000
    assert candle.close_time == 1760380859999


async def test_same_candle_cannot_be_stored_twice(session):
    session.add(make_candle())
    await session.commit()

    session.add(make_candle())
    with pytest.raises(IntegrityError):
        await session.commit()



async def test_rest_klines_are_parsed_into_decimals():
    fake = FakeBinance()
    client = make_rest_client(fake)

    candles = await client.get_klines("BTCEUR", "1m", 3)

    assert fake.requests[0].url.path == "/api/v3/klines"
    assert fake.requests[0].url.params["symbol"] == "BTCEUR"
    assert len(candles) == 3
    first = candles[0]
    assert first.symbol == "BTCEUR"
    assert first.interval == "1m"
    assert first.open_time == datetime(2025, 10, 13, 18, 40, tzinfo=UTC)
    assert first.open == Decimal("72300.10000000")
    assert str(first.close) == "72416.85000000"
    assert first.volume == Decimal("18.23455100")
    # Only the newest candle is still open.
    assert [candle.is_closed for candle in candles] == [True, True, False]
    await client.close()


async def test_invalid_rest_rows_are_dropped_not_zeroed():
    rows = [binance_row(0), binance_row(1, close="0"), binance_row(2, close="abc")]
    client = make_rest_client(FakeBinance(rows=rows))

    candles = await client.get_klines("BTCEUR", "1m", 3)

    assert len(candles) == 1
    assert candles[0].close == Decimal("72416.85000000")
    await client.close()


@pytest.mark.parametrize("status_code", [500, 503])
async def test_server_errors_raise_exchange_unavailable(status_code):
    client = make_rest_client(FakeBinance(status_code=status_code))

    with pytest.raises(ExchangeUnavailableError):
        await client.get_klines("BTCEUR", "1m", 3)
    await client.close()


async def test_network_error_raises_exchange_unavailable():
    def fail(request):
        raise httpx.ConnectError("no network")

    client = BinanceRestClient(SETTINGS, transport=httpx.MockTransport(fail))

    with pytest.raises(ExchangeUnavailableError):
        await client.get_klines("BTCEUR", "1m", 3)
    await client.close()


@pytest.mark.parametrize("status_code", [418, 429])
async def test_rate_limit_pauses_further_requests(status_code):
    fake = FakeBinance(status_code=status_code, headers={"Retry-After": "120"})
    client = make_rest_client(fake)

    with pytest.raises(ExchangeUnavailableError):
        await client.get_klines("BTCEUR", "1m", 3)
    with pytest.raises(ExchangeUnavailableError):
        await client.get_klines("BTCEUR", "1m", 3)

    # The second call never reached Binance.
    assert len(fake.requests) == 1
    await client.close()


async def test_double_upsert_keeps_one_row(session_factory):
    service = CandleService(make_rest_client(FakeBinance()), session_factory)

    await service.upsert_candles([make_kline(close="72416.85")])
    await service.upsert_candles([make_kline(close="72426.00")])

    assert await count_candles(session_factory) == 1
    async with session_factory() as session:
        stored = (await session.scalars(select(Candle))).one()
    assert stored.close_price == Decimal("72426.00")
    assert stored.open_time == FIRST_OPEN_MS
    assert stored.close_time == FIRST_OPEN_MS + MINUTE_MS - 1


async def test_only_closed_1m_klines_are_stored(session_factory):
    service = CandleService(make_rest_client(FakeBinance()), session_factory)

    await service.handle_event({"type": "kline", "data": make_kline(is_closed=False)})
    assert await count_candles(session_factory) == 0

    await service.handle_event({"type": "kline", "data": make_kline(interval="5m")})
    assert await count_candles(session_factory) == 0

    await service.handle_event({"type": "kline", "data": make_kline(is_closed=True)})
    assert await count_candles(session_factory) == 1


async def test_1m_history_is_saved_without_the_open_candle(session_factory):
    service = CandleService(make_rest_client(FakeBinance()), session_factory)

    candles = await service.get_candles("BTCEUR", "1m", 3)

    assert len(candles) == 3
    assert await count_candles(session_factory) == 2


async def test_1m_candles_come_from_db_when_binance_returns_503(session_factory):
    service = CandleService(make_rest_client(FakeBinance()), session_factory)
    await service.upsert_candles([make_kline()])
    service.rest_client = make_rest_client(FakeBinance(status_code=503))

    candles = await service.get_candles("BTCEUR", "1m", 300)

    assert len(candles) == 1
    assert candles[0].close == Decimal("72416.85")
    assert candles[0].open_time == datetime(2025, 10, 13, 18, 40, tzinfo=UTC)


async def test_other_intervals_fail_when_binance_returns_503(session_factory):
    service = CandleService(make_rest_client(FakeBinance(status_code=503)), session_factory)

    with pytest.raises(ExchangeUnavailableError):
        await service.get_candles("BTCEUR", "15m", 50)


async def test_responses_are_cached_for_15_seconds(session_factory):
    now = [1000.0]
    fake = FakeBinance()
    service = CandleService(make_rest_client(fake), session_factory, clock=lambda: now[0])

    await service.get_candles("BTCEUR", "15m", 3)
    now[0] += 10
    cached = await service.get_candles("BTCEUR", "15m", 2)
    assert len(fake.requests) == 1
    assert len(cached) == 2

    now[0] += 6
    await service.get_candles("BTCEUR", "15m", 3)
    assert len(fake.requests) == 2


@pytest.fixture
def lifespan_starts(monkeypatch):
    """Fail loudly if anything starts a real MarketDataService (and so a Binance connection)."""
    starts = []

    async def forbidden_start(self):
        starts.append(self)
        raise AssertionError("Tests must not start the real Binance connection")

    monkeypatch.setattr(MarketDataService, "start", forbidden_start)
    return starts


@pytest.fixture
def api_client(session_factory, lifespan_starts):
    fake_binance = FakeBinance(rows=[binance_row(i) for i in range(60)])
    candle_service = CandleService(make_rest_client(fake_binance), session_factory)
    market_service = MarketDataService(SETTINGS)  # never started: no Binance connection

    original_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_market_service] = lambda: market_service
    app.dependency_overrides[get_candle_service] = lambda: candle_service
    # No `with` block: the lifespan (real Binance client, real database) does not start.
    yield TestClient(app)
    app.dependency_overrides.clear()
    app.dependency_overrides.update(original_overrides)
    assert lifespan_starts == []


def test_candles_endpoint_returns_chart_format(api_client):
    response = api_client.get("/api/markets/btc-eur/candles?interval=15m&limit=50")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 50
    assert body[0] == {
        "time": (FIRST_OPEN_MS + 10 * MINUTE_MS) // 1000,
        "open": "72300.10000000",
        "high": "72480.00000000",
        "low": "72250.01000000",
        "close": "72416.85000000",
        "volume": "18.23455100",
    }
    assert [candle["time"] for candle in body] == sorted(candle["time"] for candle in body)


@pytest.mark.parametrize("query", ["interval=3m", "limit=5000", "limit=0", "limit=abc"])
def test_candles_endpoint_rejects_invalid_params(api_client, query):
    assert api_client.get(f"/api/markets/BTCEUR/candles?{query}").status_code == 422


def test_candles_endpoint_unknown_symbol_returns_404(api_client):
    assert api_client.get("/api/markets/DOGEEUR/candles").status_code == 404
