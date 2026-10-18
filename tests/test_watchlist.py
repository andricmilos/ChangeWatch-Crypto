from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.dependencies import get_market_service
from app.config import Settings
from app.database import Base, get_session
from app.main import app
from app.models import WatchlistItem
from app.services.market_data_service import MarketDataService


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    # In-memory SQLite (StaticPool: one shared connection), so tests never touch
    # data/changewatch.db. Each request still gets its own session, like in the app.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


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
def client(session_factory, lifespan_starts):
    market_service = MarketDataService(Settings(_env_file=None))  # never started

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
    assert lifespan_starts == []


def symbols(client) -> list[str]:
    return [item["symbol"] for item in client.get("/api/watchlist").json()]


def test_empty_watchlist(client):
    response = client.get("/api/watchlist")

    assert response.status_code == 200
    assert response.json() == []


def test_add_pair(client):
    response = client.post("/api/watchlist", json={"symbol": "btc-eur"})

    assert response.status_code == 201
    body = response.json()
    assert body["symbol"] == "BTCEUR"
    assert body["id"] > 0
    assert body["created_at"]


def test_list_keeps_insertion_order(client):
    for symbol in ["SOLEUR", "BTCEUR", "ETHEUR"]:
        client.post("/api/watchlist", json={"symbol": symbol})

    assert symbols(client) == ["SOLEUR", "BTCEUR", "ETHEUR"]


async def test_duplicate_returns_409_and_keeps_one_row(client, session_factory):
    assert client.post("/api/watchlist", json={"symbol": "BTCEUR"}).status_code == 201

    response = client.post("/api/watchlist", json={"symbol": "btceur"})

    assert response.status_code == 409
    assert response.json()["detail"] == "Par je već u watchlist-i"
    async with session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(WatchlistItem))
    assert count == 1


async def test_database_rejects_duplicate_symbol(session):
    # The last line of defence: even without the API, the table refuses a second row.
    session.add(WatchlistItem(symbol="BTCEUR"))
    await session.commit()

    session.add(WatchlistItem(symbol="BTCEUR"))
    with pytest.raises(IntegrityError):
        await session.commit()


@pytest.mark.parametrize("payload", [{"symbol": "DOGEEUR"}, {"symbol": ""}, {}])
def test_invalid_symbol_returns_422(client, payload):
    assert client.post("/api/watchlist", json=payload).status_code == 422
    assert symbols(client) == []


def test_delete_pair(client):
    client.post("/api/watchlist", json={"symbol": "BTCEUR"})
    client.post("/api/watchlist", json={"symbol": "SOLEUR"})

    response = client.delete("/api/watchlist/btceur")

    assert response.status_code == 204
    assert response.content == b""
    assert symbols(client) == ["SOLEUR"]


def test_delete_missing_pair_returns_404(client):
    assert client.delete("/api/watchlist/ETHEUR").status_code == 404


def test_delete_unknown_symbol_returns_404(client):
    assert client.delete("/api/watchlist/DOGEEUR").status_code == 404


async def test_new_session_sees_saved_pair(client, session_factory):
    client.post("/api/watchlist", json={"symbol": "SOLEUR"})

    # A fresh session, like after a server restart, reads the row back from the database.
    async with session_factory() as session:
        saved = (await session.scalars(select(WatchlistItem))).all()

    assert [item.symbol for item in saved] == ["SOLEUR"]
