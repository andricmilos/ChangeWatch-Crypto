from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.dependencies import (
    get_connection_manager,
    get_market_service,
    get_orderbook_service,
)
from app.api.routes_notes import MAX_NOTES
from app.config import Settings
from app.database import Base, get_session
from app.main import app
from app.models import Note
from app.services.market_data_service import MarketDataService
from app.services.orderbook_service import OrderBookService
from app.websocket.manager import ConnectionManager

MINUTE_MS = 60_000


def now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


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
def settings() -> Settings:
    return Settings(_env_file=None)


@pytest.fixture
def client(session_factory, lifespan_starts, settings):
    market_service = MarketDataService(settings)  # never started
    manager = ConnectionManager()

    async def test_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    original_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_market_service] = lambda: market_service
    app.dependency_overrides[get_connection_manager] = lambda: manager
    app.dependency_overrides[get_orderbook_service] = lambda: OrderBookService(settings)
    app.dependency_overrides[get_session] = test_session
    # No `with` block: the lifespan (real Binance client, real database) does not start.
    yield TestClient(app)
    app.dependency_overrides.clear()
    app.dependency_overrides.update(original_overrides)
    assert lifespan_starts == []


def create_note(client, text="Breakout", time_ms=None, symbol="BTCEUR"):
    payload = {"symbol": symbol, "time_ms": now_ms() if time_ms is None else time_ms, "text": text}
    return client.post("/api/notes", json=payload)


def texts(client, **params) -> list[str]:
    response = client.get("/api/notes", params={"symbol": "BTCEUR", **params})
    assert response.status_code == 200
    return [note["text"] for note in response.json()]


# ---------- CRUD ----------


def test_create_note(client):
    time_ms = now_ms() - MINUTE_MS

    response = create_note(
        client, text="  Breakout above range  ", time_ms=time_ms, symbol="btc-eur"
    )

    assert response.status_code == 201
    body = response.json()
    assert body["id"] > 0
    assert body["symbol"] == "BTCEUR"
    assert body["time_ms"] == time_ms
    assert body["text"] == "Breakout above range"
    assert body["created_at"] and body["updated_at"]


def test_update_note_changes_only_text(client):
    created = create_note(client, text="Old").json()

    response = client.patch(f"/api/notes/{created['id']}", json={"text": " New "})

    assert response.status_code == 200
    body = response.json()
    assert body["text"] == "New"
    assert body["time_ms"] == created["time_ms"]
    assert body["symbol"] == created["symbol"]
    assert body["updated_at"] >= created["updated_at"]
    assert texts(client) == ["New"]


def test_delete_note(client):
    first = create_note(client, text="First").json()
    create_note(client, text="Second")

    response = client.delete(f"/api/notes/{first['id']}")

    assert response.status_code == 204
    assert response.content == b""
    assert texts(client) == ["Second"]


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [("patch", {"json": {"text": "New"}}), ("delete", {})],
)
def test_missing_note_returns_404(client, method, kwargs):
    response = getattr(client, method)("/api/notes/999", **kwargs)

    assert response.status_code == 404
    assert response.json()["detail"] == "Beleška ne postoji"


# ---------- Listing ----------


def test_list_is_sorted_by_time_and_filtered_by_symbol(client):
    now = now_ms()
    create_note(client, text="Latest", time_ms=now - 1 * MINUTE_MS)
    create_note(client, text="Oldest", time_ms=now - 3 * MINUTE_MS)
    create_note(client, text="Middle", time_ms=now - 2 * MINUTE_MS)
    create_note(client, text="Other pair", time_ms=now - 2 * MINUTE_MS, symbol="ETHEUR")

    assert texts(client) == ["Oldest", "Middle", "Latest"]


def test_list_filters_by_period_inclusive(client):
    now = now_ms()
    for minutes in (4, 3, 2, 1):
        create_note(client, text=f"{minutes}m ago", time_ms=now - minutes * MINUTE_MS)

    assert texts(client, from_ms=now - 3 * MINUTE_MS, to_ms=now - 2 * MINUTE_MS) == [
        "3m ago",
        "2m ago",
    ]
    assert texts(client, from_ms=now - 2 * MINUTE_MS) == ["2m ago", "1m ago"]
    assert texts(client, to_ms=now - 4 * MINUTE_MS) == ["4m ago"]


def test_list_accepts_equal_from_and_to(client):
    time_ms = now_ms() - MINUTE_MS
    create_note(client, text="Exact", time_ms=time_ms)

    assert texts(client, from_ms=time_ms, to_ms=time_ms) == ["Exact"]


def test_list_rejects_from_after_to(client):
    response = client.get("/api/notes", params={"symbol": "BTCEUR", "from_ms": 2, "to_ms": 1})

    assert response.status_code == 422


def test_list_unknown_symbol_returns_404(client):
    # Same as other symbols outside the path/body rule (SPEC 16.6): normalize_symbol gives 404.
    assert client.get("/api/notes", params={"symbol": "DOGEEUR"}).status_code == 404


def test_list_requires_symbol(client):
    assert client.get("/api/notes").status_code == 422


# ---------- Validation ----------


@pytest.mark.parametrize(
    "text",
    ["", "   ", "x" * 281, "<script>alert(1)</script>", "Look <b>here</b>"],
)
def test_invalid_text_returns_422(client, text):
    assert create_note(client, text=text).status_code == 422
    assert texts(client) == []


def test_text_of_280_characters_after_trim_is_accepted(client):
    response = create_note(client, text="  " + "x" * 280 + "  ")

    assert response.status_code == 201
    assert len(response.json()["text"]) == 280


def test_lone_angle_brackets_are_plain_text(client):
    assert create_note(client, text="BTC > 70k, ETH < 3k").status_code == 201


@pytest.mark.parametrize("text", ["", "x" * 281, "<i>no</i>"])
def test_update_with_invalid_text_returns_422(client, text):
    created = create_note(client, text="Keep").json()

    assert client.patch(f"/api/notes/{created['id']}", json={"text": text}).status_code == 422
    assert texts(client) == ["Keep"]


def test_time_in_the_future_returns_422(client):
    assert create_note(client, time_ms=now_ms() + 5 * MINUTE_MS).status_code == 422


def test_time_slightly_ahead_is_within_tolerance(client):
    # The browser clock may run a few seconds ahead of the server.
    assert create_note(client, time_ms=now_ms() + 30_000).status_code == 201


def test_time_older_than_one_year_returns_422(client):
    too_old = now_ms() - int(timedelta(days=366).total_seconds() * 1000)

    assert create_note(client, time_ms=too_old).status_code == 422


def test_time_just_under_one_year_is_accepted(client):
    old = now_ms() - int(timedelta(days=364).total_seconds() * 1000)

    assert create_note(client, time_ms=old).status_code == 201


@pytest.mark.parametrize("symbol", ["DOGEEUR", ""])
def test_unknown_symbol_returns_422(client, symbol):
    assert create_note(client, symbol=symbol).status_code == 422


# ---------- Limit ----------


async def test_create_over_limit_returns_400(client, session_factory):
    # Filled directly in the database: 500 POSTs would hit the rate limiter first.
    time_ms = now_ms() - MINUTE_MS
    async with session_factory() as session:
        session.add_all(
            Note(symbol="BTCEUR", time_ms=time_ms, text=f"n{i}") for i in range(MAX_NOTES - 1)
        )
        await session.commit()

    assert create_note(client, text="Last allowed").status_code == 201

    response = create_note(client, text="One too many")

    assert response.status_code == 400
    assert "500" in response.json()["detail"]
    assert len(texts(client)) == MAX_NOTES


# ---------- NOTES_WRITE_ENABLED ----------


def test_config_reports_write_enabled(client):
    assert client.get("/api/notes/config").json() == {"write_enabled": True}


@pytest.mark.parametrize("settings", [Settings(_env_file=None, notes_write_enabled=False)])
async def test_read_only_server_rejects_writes_but_lists(client, session_factory, settings):
    async with session_factory() as session:
        note = Note(symbol="BTCEUR", time_ms=now_ms() - MINUTE_MS, text="Existing")
        session.add(note)
        await session.commit()

    assert client.get("/api/notes/config").json() == {"write_enabled": False}
    assert create_note(client).status_code == 403
    assert client.patch(f"/api/notes/{note.id}", json={"text": "New"}).status_code == 403
    assert client.delete(f"/api/notes/{note.id}").status_code == 403
    assert texts(client) == ["Existing"]


# ---------- Live updates ----------


def test_changes_are_sent_to_browsers(client):
    with client.websocket_connect("/ws/market") as websocket:
        assert websocket.receive_json()["type"] == "snapshot"

        created = create_note(client, text="Hello").json()
        assert websocket.receive_json() == {"type": "note_created", "data": created}

        updated = client.patch(f"/api/notes/{created['id']}", json={"text": "Bye"}).json()
        assert websocket.receive_json() == {"type": "note_updated", "data": updated}

        client.delete(f"/api/notes/{created['id']}")
        assert websocket.receive_json() == {
            "type": "note_deleted",
            "data": {"id": created["id"], "symbol": "BTCEUR"},
        }


def test_rejected_change_sends_nothing(client):
    manager = app.dependency_overrides[get_connection_manager]()
    with client.websocket_connect("/ws/market") as websocket:
        websocket.receive_json()
        create_note(client, text="")
        client.delete("/api/notes/999")

    assert manager.stats.browser_messages_sent == 1  # only the snapshot
