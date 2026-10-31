from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.dependencies import (
    get_connection_manager,
    get_market_service,
    get_orderbook_service,
)
from app.config import Settings
from app.database import Base, get_session
from app.main import app
from app.models import PairTag, Tag
from app.services.market_data_service import MarketDataService
from app.services.orderbook_service import OrderBookService
from app.services.pair_meta_service import MAX_TAGS
from app.websocket.manager import ConnectionManager


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


def create_tag(client, name="Pratim", color="blue"):
    return client.post("/api/tags", json={"name": name, "color": color})


def tag_names(client) -> list[str]:
    response = client.get("/api/tags")
    assert response.status_code == 200
    return [tag["name"] for tag in response.json()]


def set_tags(client, tag_ids, symbol="BTCEUR"):
    return client.put(f"/api/pairs/{symbol}/tags", json={"tag_ids": tag_ids})


def set_note(client, text, symbol="BTCEUR"):
    return client.put(f"/api/pairs/{symbol}/note", json={"text": text})


def meta_of(client, symbol="BTCEUR") -> dict:
    response = client.get("/api/pairs/meta")
    assert response.status_code == 200
    return next(meta for meta in response.json() if meta["symbol"] == symbol)


def pair_tag_names(client, symbol="BTCEUR") -> list[str]:
    return [tag["name"] for tag in meta_of(client, symbol)["tags"]]


# ---------- Tag CRUD ----------


def test_create_tag(client):
    response = create_tag(client, name="  Dugoročno  ", color="green")

    assert response.status_code == 201
    body = response.json()
    assert body["id"] > 0
    assert body["name"] == "Dugoročno"
    assert body["color"] == "green"
    assert body["created_at"]


def test_list_tags_sorted_by_name_with_pair_count(client):
    rizicno = create_tag(client, name="Rizično").json()
    dugorocno = create_tag(client, name="dugoročno").json()
    create_tag(client, name="Pratim")
    set_tags(client, [rizicno["id"], dugorocno["id"]], symbol="BTCEUR")
    set_tags(client, [rizicno["id"]], symbol="ETHEUR")

    body = client.get("/api/tags").json()

    assert [(tag["name"], tag["pair_count"]) for tag in body] == [
        ("dugoročno", 1),
        ("Pratim", 0),
        ("Rizično", 2),
    ]


def test_update_tag_name_and_color(client):
    tag = create_tag(client, name="Old", color="red").json()

    renamed = client.patch(f"/api/tags/{tag['id']}", json={"name": "New"})
    recolored = client.patch(f"/api/tags/{tag['id']}", json={"color": "teal"})

    assert renamed.status_code == 200
    assert renamed.json()["name"] == "New"
    assert renamed.json()["color"] == "red"
    assert recolored.json() == {**renamed.json(), "color": "teal"}


def test_update_tag_may_change_case_of_its_own_name(client):
    tag = create_tag(client, name="pratim").json()

    response = client.patch(f"/api/tags/{tag['id']}", json={"name": "PRATIM"})

    assert response.status_code == 200
    assert tag_names(client) == ["PRATIM"]


def test_update_without_fields_returns_422(client):
    tag = create_tag(client).json()

    assert client.patch(f"/api/tags/{tag['id']}", json={}).status_code == 422


def test_delete_tag(client):
    first = create_tag(client, name="First").json()
    create_tag(client, name="Second")

    response = client.delete(f"/api/tags/{first['id']}")

    assert response.status_code == 204
    assert response.content == b""
    assert tag_names(client) == ["Second"]


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [("patch", {"json": {"color": "red"}}), ("delete", {})],
)
def test_missing_tag_returns_404(client, method, kwargs):
    response = getattr(client, method)("/api/tags/999", **kwargs)

    assert response.status_code == 404
    assert response.json()["detail"] == "Oznaka ne postoji"


@pytest.mark.parametrize("name", ["Pratim", "PRATIM", " pratim "])
def test_duplicate_name_ignoring_case_returns_409(client, name):
    create_tag(client, name="Pratim")

    response = create_tag(client, name=name, color="red")

    assert response.status_code == 409
    assert tag_names(client) == ["Pratim"]


def test_duplicate_name_with_non_ascii_letters_returns_409(client):
    # SQLite's lower() only folds ASCII; the name_key column uses Python's casefold().
    create_tag(client, name="Rizično")

    assert create_tag(client, name="RIZIČNO").status_code == 409


def test_rename_to_existing_name_returns_409(client):
    create_tag(client, name="Pratim")
    other = create_tag(client, name="Rizično").json()

    response = client.patch(f"/api/tags/{other['id']}", json={"name": "pratim"})

    assert response.status_code == 409
    assert tag_names(client) == ["Pratim", "Rizično"]


# ---------- Tag validation ----------


@pytest.mark.parametrize("color", ["pink", "GREEN", "#00ff00", ""])
def test_invalid_color_returns_422(client, color):
    assert create_tag(client, color=color).status_code == 422
    assert tag_names(client) == []


@pytest.mark.parametrize("name", ["", "   ", "x" * 21])
def test_invalid_name_returns_422(client, name):
    assert create_tag(client, name=name).status_code == 422
    assert tag_names(client) == []


def test_name_of_20_characters_after_trim_is_accepted(client):
    response = create_tag(client, name=" " + "x" * 20 + " ")

    assert response.status_code == 201
    assert response.json()["name"] == "x" * 20


def test_update_with_invalid_values_returns_422(client):
    tag = create_tag(client, name="Keep").json()

    assert client.patch(f"/api/tags/{tag['id']}", json={"name": ""}).status_code == 422
    assert client.patch(f"/api/tags/{tag['id']}", json={"color": "pink"}).status_code == 422
    assert client.get("/api/tags").json() == [{**tag, "pair_count": 0}]


# ---------- Tag limit ----------


async def test_create_over_tag_limit_returns_400(client, session_factory):
    async with session_factory() as session:
        session.add_all(
            Tag(name=f"t{i}", name_key=f"t{i}", color="gray") for i in range(MAX_TAGS - 1)
        )
        await session.commit()

    assert create_tag(client, name="Last allowed").status_code == 201

    response = create_tag(client, name="One too many")

    assert response.status_code == 400
    assert "20" in response.json()["detail"]
    assert len(tag_names(client)) == MAX_TAGS


# ---------- Pair tags ----------


def test_meta_lists_every_supported_pair_in_order(client, settings):
    body = client.get("/api/pairs/meta").json()

    assert [meta["symbol"] for meta in body] == settings.supported_symbols
    assert all(meta["tags"] == [] and meta["note"] is None for meta in body)


def test_assign_tags_to_pair(client):
    pratim = create_tag(client, name="Pratim").json()
    rizicno = create_tag(client, name="Rizično", color="red").json()

    response = set_tags(client, [rizicno["id"], pratim["id"]], symbol="btc-eur")

    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "BTCEUR"
    assert body["tags"] == [pratim, rizicno]
    assert body["note"] is None
    assert meta_of(client) == body
    assert pair_tag_names(client, "ETHEUR") == []


def test_assign_replaces_whole_set(client):
    a, b, c = (create_tag(client, name=name).json() for name in ("A", "B", "C"))
    set_tags(client, [a["id"], b["id"]])

    assert set_tags(client, [c["id"]]).json()["tags"] == [c]
    assert pair_tag_names(client) == ["C"]
    assert set_tags(client, []).json()["tags"] == []


def test_assign_unknown_tag_id_returns_422_and_changes_nothing(client):
    tag = create_tag(client).json()
    set_tags(client, [tag["id"]])

    response = set_tags(client, [tag["id"], 999])

    assert response.status_code == 422
    assert "999" in response.json()["detail"]
    assert pair_tag_names(client) == ["Pratim"]


def test_duplicate_ids_are_ignored(client):
    tag = create_tag(client).json()

    response = set_tags(client, [tag["id"], tag["id"], tag["id"]])

    assert response.status_code == 200
    assert response.json()["tags"] == [tag]


def test_more_than_five_tags_per_pair_returns_400(client):
    ids = [create_tag(client, name=f"T{i}").json()["id"] for i in range(6)]

    assert set_tags(client, ids[:5] + ids[:5]).status_code == 200

    response = set_tags(client, ids)

    assert response.status_code == 400
    assert "5" in response.json()["detail"]
    assert pair_tag_names(client) == ["T0", "T1", "T2", "T3", "T4"]


@pytest.mark.parametrize("body", [{}, {"tag_ids": "1"}, {"tag_ids": ["a"]}])
def test_invalid_tags_body_returns_422(client, body):
    assert client.put("/api/pairs/BTCEUR/tags", json=body).status_code == 422


def test_delete_tag_removes_it_from_every_pair(client, session_factory):
    keep = create_tag(client, name="Keep").json()
    gone = create_tag(client, name="Gone").json()
    set_tags(client, [keep["id"], gone["id"]], symbol="BTCEUR")
    set_tags(client, [gone["id"]], symbol="ETHEUR")

    assert client.delete(f"/api/tags/{gone['id']}").status_code == 204

    assert pair_tag_names(client, "BTCEUR") == ["Keep"]
    assert pair_tag_names(client, "ETHEUR") == []


async def test_delete_tag_leaves_no_links_in_database(client, session_factory):
    tag = create_tag(client).json()
    set_tags(client, [tag["id"]])

    client.delete(f"/api/tags/{tag['id']}")

    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(PairTag)) == 0


# ---------- Pair notes ----------


def test_note_create_update_and_clear(client):
    created = set_note(client, "  Holding long term  ")
    assert created.status_code == 200
    assert created.json()["note"]["text"] == "Holding long term"
    assert created.json()["note"]["updated_at"]

    updated = set_note(client, "Sold half")
    assert updated.json()["note"]["text"] == "Sold half"
    assert updated.json()["note"]["updated_at"] >= created.json()["note"]["updated_at"]
    assert meta_of(client)["note"] == updated.json()["note"]

    for empty in ("", "   "):
        cleared = set_note(client, empty)
        assert cleared.status_code == 200
        assert cleared.json()["note"] is None
        assert meta_of(client)["note"] is None


def test_note_belongs_to_one_pair(client):
    set_note(client, "Only BTC", symbol="BTCEUR")

    assert meta_of(client, "ETHEUR")["note"] is None


def test_note_of_500_characters_after_trim_is_accepted(client):
    response = set_note(client, " " + "x" * 500 + " ")

    assert response.status_code == 200
    assert len(response.json()["note"]["text"]) == 500


def test_too_long_note_returns_422(client):
    set_note(client, "Keep")

    assert set_note(client, "x" * 501).status_code == 422
    assert meta_of(client)["note"]["text"] == "Keep"


def test_note_without_text_returns_422(client):
    assert client.put("/api/pairs/BTCEUR/note", json={}).status_code == 422


# ---------- Unknown symbol ----------


@pytest.mark.parametrize(
    ("path", "body"),
    [("/api/pairs/DOGEEUR/tags", {"tag_ids": []}), ("/api/pairs/DOGEEUR/note", {"text": "x"})],
)
def test_unknown_symbol_returns_404(client, path, body):
    response = client.put(path, json=body)

    assert response.status_code == 404
    assert response.json()["detail"] == "Unknown symbol: DOGEEUR"


# ---------- NOTES_WRITE_ENABLED ----------


@pytest.mark.parametrize("settings", [Settings(_env_file=None, notes_write_enabled=False)])
async def test_read_only_server_rejects_writes_but_reads(client, session_factory, settings):
    async with session_factory() as session:
        tag = Tag(name="Existing", name_key="existing", color="gray")
        session.add(tag)
        await session.commit()

    assert create_tag(client).status_code == 403
    assert client.patch(f"/api/tags/{tag.id}", json={"color": "red"}).status_code == 403
    assert client.delete(f"/api/tags/{tag.id}").status_code == 403
    assert set_tags(client, [tag.id]).status_code == 403
    assert set_note(client, "No").status_code == 403
    # The switch is checked before the symbol, like before the rate limiter.
    assert set_note(client, "No", symbol="DOGEEUR").status_code == 403

    assert tag_names(client) == ["Existing"]
    assert meta_of(client) == {"symbol": "BTCEUR", "tags": [], "note": None}
    assert client.get("/api/notes/config").json() == {"write_enabled": False}


# ---------- Live updates ----------


def all_tags(client) -> list[dict]:
    return client.get("/api/tags").json()


def test_tag_changes_send_tags_updated(client):
    with client.websocket_connect("/ws/market") as websocket:
        assert websocket.receive_json()["type"] == "snapshot"

        created = create_tag(client, name="Pratim").json()
        message = websocket.receive_json()
        assert message == {"type": "tags_updated", "data": all_tags(client)}
        assert message["data"][0]["id"] == created["id"]

        client.patch(f"/api/tags/{created['id']}", json={"color": "red"})
        message = websocket.receive_json()
        assert message["type"] == "tags_updated"
        assert message["data"][0]["color"] == "red"

        client.delete(f"/api/tags/{created['id']}")
        assert websocket.receive_json() == {"type": "tags_updated", "data": []}


def test_pair_changes_send_pair_meta_updated(client):
    tag = create_tag(client).json()
    with client.websocket_connect("/ws/market") as websocket:
        websocket.receive_json()

        meta = set_tags(client, [tag["id"]]).json()
        assert websocket.receive_json() == {"type": "pair_meta_updated", "data": meta}
        assert meta["tags"] == [tag]

        meta = set_note(client, "Hello").json()
        assert websocket.receive_json() == {"type": "pair_meta_updated", "data": meta}
        assert meta["note"]["text"] == "Hello"


def test_deleting_tag_sends_pair_meta_for_each_pair_that_had_it(client):
    keep = create_tag(client, name="Keep").json()
    gone = create_tag(client, name="Gone").json()
    set_tags(client, [keep["id"], gone["id"]], symbol="ETHEUR")
    set_tags(client, [gone["id"]], symbol="BTCEUR")
    set_tags(client, [keep["id"]], symbol="SOLEUR")
    with client.websocket_connect("/ws/market") as websocket:
        websocket.receive_json()

        client.delete(f"/api/tags/{gone['id']}")

        assert websocket.receive_json()["type"] == "tags_updated"
        first = websocket.receive_json()
        second = websocket.receive_json()

    assert first == {
        "type": "pair_meta_updated",
        "data": {"symbol": "BTCEUR", "tags": [], "note": None},
    }
    assert second == {
        "type": "pair_meta_updated",
        "data": {"symbol": "ETHEUR", "tags": [keep], "note": None},
    }


def test_rejected_change_sends_nothing(client):
    manager = app.dependency_overrides[get_connection_manager]()
    create_tag(client, name="Pratim")
    with client.websocket_connect("/ws/market") as websocket:
        websocket.receive_json()
        create_tag(client, name="pratim")
        create_tag(client, color="pink")
        client.delete("/api/tags/999")
        set_tags(client, [999])
        set_note(client, "x" * 501)
        set_note(client, "x", symbol="DOGEEUR")

    assert manager.stats.browser_messages_sent == 1  # only the snapshot
