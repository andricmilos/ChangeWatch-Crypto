import csv
import io
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api import routes_export
from app.api.dependencies import get_market_service
from app.api.rate_limit import export_limiter
from app.config import Settings
from app.database import Base, get_session
from app.main import app
from app.models import Candle, Note, PriceAlert
from app.services.market_data_service import MarketDataService

MINUTE_MS = 60_000
DAY_MS = 24 * 60 * MINUTE_MS
# 2026-10-28T12:00:00Z
START_MS = 1_793_188_800_000


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    # In-memory SQLite (StaticPool: one shared connection), so tests never touch
    # data/changewatch.db.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def client(session_factory, monkeypatch):
    market_service = MarketDataService(Settings(_env_file=None))  # never started
    monkeypatch.setattr(routes_export, "today_utc", lambda: "2026-10-28")

    async def test_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    original_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_market_service] = lambda: market_service
    app.dependency_overrides[get_session] = test_session
    # No `with` block: the lifespan (real Binance client, real database) does not start.
    yield TestClient(app)
    app.dependency_overrides.clear()
    app.dependency_overrides.update(original_overrides)


async def add(session_factory, *rows) -> None:
    async with session_factory() as session:
        session.add_all(rows)
        await session.commit()


def candle(open_time: int, close: str = "72309.76", symbol: str = "BTCEUR") -> Candle:
    return Candle(
        symbol=symbol,
        interval="1m",
        open_time=open_time,
        close_time=open_time + MINUTE_MS - 1,
        open_price=Decimal("72300.00000001"),
        high_price=Decimal("72400.12345678"),
        low_price=Decimal("72250.5"),
        close_price=Decimal(close),
        volume=Decimal("0.00000000"),
    )


def rows(response) -> list[list[str]]:
    return list(csv.reader(io.StringIO(response.text)))


def candles_csv(client, **params):
    query = {"symbol": "BTCEUR", "interval": "1m", "from_ms": START_MS, "to_ms": START_MS + DAY_MS}
    return client.get("/api/export/candles.csv", params={**query, **params})


async def test_candles_have_exact_columns_order_and_decimals(client, session_factory):
    await add(
        session_factory,
        candle(START_MS + MINUTE_MS, close="72310"),
        candle(START_MS),
        candle(START_MS, symbol="ETHEUR"),
    )

    response = candles_csv(client)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert rows(response) == [
        ["open_time_utc", "open", "high", "low", "close", "volume"],
        [
            "2026-10-28T12:00:00Z",
            "72300.00000001",
            "72400.12345678",
            "72250.5",
            "72309.76",
            "0.00000000",
        ],
        [
            "2026-10-28T12:01:00Z",
            "72300.00000001",
            "72400.12345678",
            "72250.5",
            "72310",
            "0.00000000",
        ],
    ]


async def test_candle_decimals_never_use_exponent_form(client, session_factory):
    row = candle(START_MS)
    row.volume = Decimal("0E-8")
    row.close_price = Decimal("1E+5")
    await add(session_factory, row)

    [_, values] = rows(candles_csv(client))

    assert values[4:] == ["100000", "0.00000000"]


async def test_candles_respect_inclusive_range(client, session_factory):
    await add(session_factory, *(candle(START_MS + i * MINUTE_MS) for i in range(5)))

    response = candles_csv(client, from_ms=START_MS + MINUTE_MS, to_ms=START_MS + 3 * MINUTE_MS)

    assert [row[0] for row in rows(response)[1:]] == [
        "2026-10-28T12:01:00Z",
        "2026-10-28T12:02:00Z",
        "2026-10-28T12:03:00Z",
    ]


def test_empty_candle_export_is_only_the_header(client):
    response = candles_csv(client)

    assert response.status_code == 200
    assert response.text == "open_time_utc,open,high,low,close,volume\r\n"


def test_candle_filename(client):
    response = candles_csv(client, symbol="btc-eur")

    assert response.headers["content-disposition"] == (
        'attachment; filename="changewatch_BTCEUR_1m_2026-10-28.csv"'
    )


@pytest.mark.parametrize(
    "params",
    [
        {"symbol": "XRPEUR"},
        {"interval": "5m"},
        {"interval": "1h"},
        {"from_ms": START_MS + 1, "to_ms": START_MS},
        {"to_ms": START_MS + 90 * DAY_MS + 1},
        {"from_ms": "abc"},
        {"from_ms": -1},
    ],
)
def test_invalid_candle_requests_are_422(client, params):
    assert candles_csv(client, **params).status_code == 422


def test_unstored_interval_has_clear_message(client):
    response = candles_csv(client, interval="5m")

    assert "se ne čuva u bazi" in response.json()["detail"]


def test_exactly_90_days_is_allowed(client):
    assert candles_csv(client, to_ms=START_MS + 90 * DAY_MS).status_code == 200


async def test_too_many_candle_rows_is_422(client, session_factory, monkeypatch):
    monkeypatch.setattr(routes_export, "MAX_ROWS", 2)
    await add(session_factory, *(candle(START_MS + i * MINUTE_MS) for i in range(3)))

    response = candles_csv(client)

    assert response.status_code == 422
    assert "Najviše 2 redova" in response.json()["detail"]


async def test_large_export_is_streamed_in_chunks(client, session_factory, monkeypatch):
    monkeypatch.setattr(routes_export, "CHUNK_ROWS", 2)
    await add(session_factory, *(candle(START_MS + i * MINUTE_MS) for i in range(5)))

    response = candles_csv(client)

    assert len(rows(response)) == 6


async def test_notes_escape_and_guard_against_formulas(client, session_factory):
    created = datetime(2026, 10, 28, 13, 0, 0, 123456, tzinfo=UTC)
    texts = [
        'Breakout, "big" one\nsecond line',
        '=HYPERLINK("http://evil")',
        "+1",
        "-5% dip",
        "@SUM(A1)",
        "BTC > 70k",
    ]
    await add(
        session_factory,
        *(
            Note(symbol="BTCEUR", time_ms=START_MS + i, text=text, created_at=created)
            for i, text in enumerate(texts)
        ),
        Note(symbol="ETHEUR", time_ms=START_MS, text="other pair", created_at=created),
    )

    response = client.get("/api/export/notes.csv", params={"symbol": "BTCEUR"})

    assert response.status_code == 200
    assert response.headers["content-disposition"] == (
        'attachment; filename="changewatch_notes_BTCEUR_2026-10-28.csv"'
    )
    assert '"Breakout, ""big"" one\nsecond line"' in response.text
    parsed = rows(response)
    assert parsed[0] == ["time_utc", "symbol", "text", "created_at"]
    assert parsed[1] == [
        "2026-10-28T12:00:00Z",
        "BTCEUR",
        'Breakout, "big" one\nsecond line',
        "2026-10-28T13:00:00.123456Z",
    ]
    assert [row[2] for row in parsed[2:]] == [
        '\'=HYPERLINK("http://evil")',
        "'+1",
        "'-5% dip",
        "'@SUM(A1)",
        "BTC > 70k",
    ]


async def test_notes_without_symbol_cover_every_pair(client, session_factory):
    await add(
        session_factory,
        Note(symbol="ETHEUR", time_ms=START_MS, text="eth"),
        Note(symbol="BTCEUR", time_ms=START_MS + 1, text="btc"),
    )

    response = client.get("/api/export/notes.csv")

    assert [row[1] for row in rows(response)[1:]] == ["ETHEUR", "BTCEUR"]
    assert 'filename="changewatch_notes_2026-10-28.csv"' in response.headers["content-disposition"]


@pytest.mark.parametrize(
    "params",
    [
        {"symbol": "XRPEUR"},
        {"from_ms": START_MS + 1, "to_ms": START_MS},
        {"from_ms": START_MS, "to_ms": START_MS + 91 * DAY_MS},
    ],
)
def test_invalid_note_requests_are_422(client, params):
    assert client.get("/api/export/notes.csv", params=params).status_code == 422


def test_empty_notes_export_is_only_the_header(client):
    response = client.get("/api/export/notes.csv", params={"symbol": "BTCEUR"})

    assert response.text == "time_utc,symbol,text,created_at\r\n"


async def test_alerts_have_exact_columns_and_decimals(client, session_factory):
    created = datetime(2026, 10, 27, 9, 30, tzinfo=UTC)
    await add(
        session_factory,
        PriceAlert(
            symbol="BTCEUR",
            condition="above",
            target_price=Decimal("75000.12345678"),
            is_active=False,
            created_at=created,
            triggered_at=datetime(2026, 10, 28, 10, 0, 1, tzinfo=UTC),
            triggered_price=Decimal("75000.20000000"),
        ),
        PriceAlert(
            symbol="ETHEUR",
            condition="below",
            target_price=Decimal("2500"),
            is_active=True,
            created_at=created,
        ),
    )

    response = client.get("/api/export/alerts.csv")

    assert response.status_code == 200
    assert response.headers["content-disposition"] == (
        'attachment; filename="changewatch_alerts_2026-10-28.csv"'
    )
    assert rows(response) == [
        [
            "symbol",
            "condition",
            "target_price",
            "is_active",
            "created_at",
            "triggered_at",
            "triggered_price",
        ],
        [
            "BTCEUR",
            "above",
            "75000.12345678",
            "false",
            "2026-10-27T09:30:00Z",
            "2026-10-28T10:00:01Z",
            "75000.20000000",
        ],
        ["ETHEUR", "below", "2500", "true", "2026-10-27T09:30:00Z", "", ""],
    ]


def test_empty_alerts_export_is_only_the_header(client):
    response = client.get("/api/export/alerts.csv")

    assert response.text == (
        "symbol,condition,target_price,is_active,created_at,triggered_at,triggered_price\r\n"
    )


def test_exports_are_rate_limited(client):
    export_limiter.max_requests = 1
    try:
        assert client.get("/api/export/alerts.csv").status_code == 200
        response = client.get("/api/export/notes.csv")
    finally:
        export_limiter.max_requests = 10

    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) >= 1
