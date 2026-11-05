from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.dependencies import (
    get_alert_service,
    get_market_service,
    get_orderbook_service,
    get_pair_stats_service,
    get_precision_service,
)
from app.config import Settings
from app.database import Base, get_session
from app.main import app
from app.models import Note, PairNote, PairTag, PriceAlert, Tag
from app.schemas.market import MarketTicker, WindowStats
from app.services.alert_service import AlertService
from app.services.exchange_client import (
    BinanceRestClient,
    ExchangeUnavailableError,
    SymbolPrecision,
    parse_window_stats,
)
from app.services.market_data_service import MarketDataService
from app.services.orderbook_service import OrderBookService
from app.services.pair_stats_service import PairStatsService
from app.services.precision_service import PrecisionService
from app.websocket.manager import ConnectionManager

SETTINGS = Settings(_env_file=None)

# Shape of a real answer from GET /api/v3/ticker?symbol=BTCEUR&windowSize=7d.
TICKER_7D = {
    "symbol": "BTCEUR",
    "priceChange": "-4197.63000000",
    "priceChangePercent": "-5.844",
    "weightedAvgPrice": "68855.88055249",
    "openPrice": "71825.27000000",
    "highPrice": "72191.68000000",
    "lowPrice": "66632.00000000",
    "lastPrice": "67627.64000000",
    "volume": "1227.61854000",
    "quoteVolume": "84528755.55426250",
    "openTime": 1793305080000,
    "closeTime": 1793909936192,
    "firstId": 177604941,
    "lastId": 177929268,
    "count": 324328,
}


class FakeBinance:
    """httpx.MockTransport handler for /api/v3/ticker; answers with the queued statuses."""

    def __init__(self, statuses: list[int] | None = None, payload: object = None) -> None:
        self.statuses = statuses or []
        self.payload = TICKER_7D if payload is None else payload
        self.requests: list[httpx.Request] = []
        self.network_down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.network_down:
            raise httpx.ConnectError("no network")
        status = self.statuses.pop(0) if self.statuses else 200
        if status != 200:
            return httpx.Response(status, text="error")
        return httpx.Response(200, json=self.payload)


# ---------- Parsing ----------


def test_parse_window_stats_reads_decimals():
    stats = parse_window_stats(TICKER_7D, "BTCEUR")

    assert stats == WindowStats(
        high=Decimal("72191.68000000"),
        low=Decimal("66632.00000000"),
        open=Decimal("71825.27000000"),
        last=Decimal("67627.64000000"),
        price_change_percent=Decimal("-5.844"),
        volume=Decimal("1227.61854000"),
    )


@pytest.mark.parametrize("bad", [None, "", "abc", "NaN", "Infinity", 1.5, True, [], {}])
def test_parse_window_stats_invalid_values_become_null(bad):
    data = {
        **TICKER_7D,
        "highPrice": bad,
        "lowPrice": bad,
        "openPrice": bad,
        "lastPrice": bad,
        "priceChangePercent": bad,
        "volume": bad,
    }

    stats = parse_window_stats(data, "BTCEUR")

    assert stats.model_dump() == dict.fromkeys(stats.model_dump(), None)


@pytest.mark.parametrize("price", ["0", "0.00000000", "-1"])
def test_parse_window_stats_non_positive_price_is_null_not_zero(price):
    stats = parse_window_stats({**TICKER_7D, "highPrice": price, "lowPrice": price}, "BTCEUR")

    assert stats.high is None
    assert stats.low is None


def test_parse_window_stats_keeps_negative_change_and_zero_volume():
    stats = parse_window_stats({**TICKER_7D, "volume": "0.00000000"}, "BTCEUR")

    assert stats.price_change_percent == Decimal("-5.844")
    assert stats.volume == Decimal("0")


@pytest.mark.parametrize(
    "payload", [[], "text", None, {**TICKER_7D, "symbol": "ETHEUR"}, {"code": -1121}]
)
def test_parse_window_stats_rejects_unexpected_payload(payload):
    with pytest.raises(ExchangeUnavailableError):
        parse_window_stats(payload, "BTCEUR")


async def test_client_requests_7d_window():
    fake = FakeBinance()
    client = BinanceRestClient(SETTINGS, transport=httpx.MockTransport(fake))

    stats = await client.get_window_stats("BTCEUR", "7d")

    assert stats.last == Decimal("67627.64000000")
    [request] = fake.requests
    assert request.url.path == "/api/v3/ticker"
    assert dict(request.url.params) == {"symbol": "BTCEUR", "windowSize": "7d"}
    await client.close()


# ---------- Cache ----------


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_stats_are_cached_for_60_seconds_per_pair():
    fake = FakeBinance()
    client = BinanceRestClient(SETTINGS, transport=httpx.MockTransport(fake))
    clock = FakeClock()
    service = PairStatsService(client.get_window_stats, clock=clock)

    first = await service.get_7d("BTCEUR")
    clock.now += 59
    second = await service.get_7d("BTCEUR")

    assert second == first
    assert len(fake.requests) == 1

    clock.now += 1
    await service.get_7d("BTCEUR")
    assert len(fake.requests) == 2
    await client.close()


async def test_cache_is_per_pair():
    calls: list[str] = []

    async def fetch(symbol: str, window: str) -> WindowStats:
        calls.append(symbol)
        return parse_window_stats({**TICKER_7D, "symbol": symbol}, symbol)

    service = PairStatsService(fetch)

    await service.get_7d("BTCEUR")
    await service.get_7d("ETHEUR")
    await service.get_7d("BTCEUR")

    assert calls == ["BTCEUR", "ETHEUR"]


async def test_failure_is_not_cached():
    fake = FakeBinance(statuses=[503])
    client = BinanceRestClient(SETTINGS, transport=httpx.MockTransport(fake))
    service = PairStatsService(client.get_window_stats)

    with pytest.raises(ExchangeUnavailableError):
        await service.get_7d("BTCEUR")
    stats = await service.get_7d("BTCEUR")

    assert stats.last == Decimal("67627.64000000")
    assert len(fake.requests) == 2
    await client.close()


# ---------- API and page ----------


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
async def market_service(monkeypatch) -> AsyncIterator[MarketDataService]:
    async def forbidden_start(self):
        raise AssertionError("Tests must not start the real Binance connection")

    monkeypatch.setattr(MarketDataService, "start", forbidden_start)
    service = MarketDataService(SETTINGS)  # never started
    service.client.status = "connected"
    service.client.last_message_at = datetime.now(UTC)
    yield service
    await service.stop()


@pytest.fixture
def binance() -> FakeBinance:
    return FakeBinance()


@pytest.fixture
async def rest_client(binance) -> AsyncIterator[BinanceRestClient]:
    client = BinanceRestClient(SETTINGS, transport=httpx.MockTransport(binance))
    yield client
    await client.close()


@pytest.fixture
async def precision() -> PrecisionService:
    async def fetch() -> dict[str, SymbolPrecision]:
        return {"BTCEUR": SymbolPrecision(price_decimals=2, quantity_decimals=5)}

    service = PrecisionService(fetch, SETTINGS.supported_symbols)
    await service.run()
    return service


@pytest.fixture
def client(session_factory, market_service, rest_client, precision):
    alert_service = AlertService(session_factory, market_service, ConnectionManager())
    pair_stats = PairStatsService(rest_client.get_window_stats)

    async def test_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    original_overrides = dict(app.dependency_overrides)
    app.dependency_overrides.update(
        {
            get_market_service: lambda: market_service,
            get_session: test_session,
            get_orderbook_service: lambda: OrderBookService(SETTINGS),
            get_precision_service: lambda: precision,
            get_pair_stats_service: lambda: pair_stats,
            get_alert_service: lambda: alert_service,
        }
    )
    # No `with` block: the lifespan (real Binance client, real database) does not start.
    yield TestClient(app)
    app.dependency_overrides.clear()
    app.dependency_overrides.update(original_overrides)


async def seed_pair_data(session_factory: async_sessionmaker[AsyncSession]) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        tag = Tag(name="Dugoročno", name_key="dugoročno", color="green")
        session.add(tag)
        await session.flush()
        session.add_all(
            [
                PairTag(symbol="BTCEUR", tag_id=tag.id),
                PairNote(symbol="BTCEUR", text="Držim do halvinga"),
                Note(symbol="BTCEUR", time_ms=1, text="Prva"),
                Note(symbol="BTCEUR", time_ms=2, text="Druga"),
                Note(symbol="ETHEUR", time_ms=3, text="Drugi par"),
                PriceAlert(symbol="BTCEUR", target_price=Decimal("80000"), condition="above"),
                PriceAlert(symbol="ETHEUR", target_price=Decimal("5000"), condition="above"),
            ]
        )
        # 12 fired alerts; only the newest 10 are returned.
        for minute in range(12):
            session.add(
                PriceAlert(
                    symbol="BTCEUR",
                    target_price=Decimal(60000 + minute),
                    condition="below",
                    is_active=False,
                    triggered_at=now - timedelta(minutes=12 - minute),
                    triggered_price=Decimal(59000 + minute),
                )
            )
        await session.commit()


async def test_summary_contains_every_part(client, session_factory, market_service):
    await seed_pair_data(session_factory)
    await market_service.handle_ticker(
        MarketTicker(
            symbol="BTCEUR",
            display_symbol="BTC/EUR",
            last_price=Decimal("67627.64"),
            bid_price=Decimal("67627.63"),
            ask_price=Decimal("67627.64"),
            spread=Decimal("0.01"),
            spread_percent=Decimal("0.000"),
            event_time=datetime.now(UTC),
        )
    )

    response = client.get("/api/pairs/BTCEUR/summary")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["market"]["symbol"] == "BTCEUR"
    assert body["market"]["last_price"] == "67627.64"
    assert body["market"]["bid_price"] == "67627.63"
    assert body["market"]["spread"] == "0.01"
    assert body["market"]["freshness"] == "live"
    assert body["stats_7d"] == {
        "high": "72191.68000000",
        "low": "66632.00000000",
        "open": "71825.27000000",
        "last": "67627.64000000",
        "price_change_percent": "-5.844",
        "volume": "1227.61854000",
    }
    assert body["stats_7d_error"] is None
    assert body["meta"] == {"symbol": "BTCEUR", "price_decimals": 2, "quantity_decimals": 5}
    assert body["orderbook"]["freshness"] == "no_data"
    assert [tag["name"] for tag in body["tags"]] == ["Dugoročno"]
    assert body["pair_note"]["text"] == "Držim do halvinga"
    assert [alert["target_price"] for alert in body["active_alerts"]] == ["80000"]
    triggered = body["triggered_alerts"]
    assert len(triggered) == 10
    # Newest first: the alert fired last has the highest target.
    assert [alert["target_price"] for alert in triggered] == [str(60011 - i) for i in range(10)]
    assert all(alert["symbol"] == "BTCEUR" for alert in triggered)
    assert body["chart_notes_count"] == 2


def test_summary_of_pair_without_data(client):
    response = client.get("/api/pairs/ADAEUR/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["market"]["last_price"] is None
    assert body["market"]["freshness"] == "no_data"
    assert body["meta"] == {"symbol": "ADAEUR", "price_decimals": None, "quantity_decimals": None}
    assert body["tags"] == []
    assert body["pair_note"] is None
    assert body["active_alerts"] == []
    assert body["triggered_alerts"] == []
    assert body["chart_notes_count"] == 0


def test_summary_accepts_other_symbol_spellings(client):
    response = client.get("/api/pairs/btc-eur/summary")

    assert response.status_code == 200
    assert response.json()["market"]["symbol"] == "BTCEUR"


@pytest.mark.parametrize("status", [503, 429])
def test_summary_without_binance_still_returns_200(client, binance, status):
    binance.statuses = [status]

    response = client.get("/api/pairs/BTCEUR/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["stats_7d"] is None
    assert body["stats_7d_error"] == "Berza trenutno nije dostupna"
    assert body["market"]["symbol"] == "BTCEUR"
    assert body["chart_notes_count"] == 0


def test_summary_survives_network_error(client, binance):
    binance.network_down = True

    response = client.get("/api/pairs/BTCEUR/summary")

    assert response.status_code == 200
    assert response.json()["stats_7d"] is None


def test_summary_unknown_symbol_is_404(client, binance):
    response = client.get("/api/pairs/XRPEUR/summary")

    assert response.status_code == 404
    assert response.json() == {"detail": "Unknown symbol: XRPEUR"}
    assert binance.requests == []


def test_pair_page_renders(client):
    response = client.get("/pair/BTCEUR")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "BTC/EUR" in response.text
    assert 'href="/"' in response.text


def test_pair_page_title_names_the_pair(client):
    html = client.get("/pair/BTCEUR").text

    assert "<title>BTC/EUR · ChangeWatch</title>" in html


def test_pair_page_shares_layout_with_dashboard(client):
    html = client.get("/pair/BTCEUR").text

    assert 'id="connection-indicator"' in html
    assert 'id="banner-server"' in html
    assert 'id="favicon"' in html
    assert "TradingView Lightweight Charts™" in html


def test_pair_page_has_quote_stats_chart_and_orderbook(client):
    html = client.get("/pair/BTCEUR").text

    assert 'id="pair-name" data-symbol="BTCEUR"' in html
    for field in ["last_price", "price_change_percent", "bid_price", "ask_price"]:
        assert f'data-field="{field}"' in html
    for field in ["high_24h", "low_24h", "volume_24h", "high", "low", "volume"]:
        assert f'data-field="{field}"' in html
    assert "7-dnevna statistika trenutno nije dostupna" in html
    assert 'id="chart"' in html
    for interval in ["1m", "5m", "15m", "1h"]:
        assert f'data-interval="{interval}"' in html
    assert 'id="orderbook-table"' in html
    # The page has its own alert form, so a click on a book price fills it in.
    assert "ciljnu cenu novog alerta" in html


def test_pair_page_has_alerts_for_this_pair(client):
    html = client.get("/pair/BTCEUR").text

    assert 'hx-post="/partials/alerts?pair=BTCEUR"' in html
    assert 'hx-get="/partials/alerts?pair=BTCEUR"' in html
    assert '<input type="hidden" name="symbol" value="BTCEUR">' in html
    # The live price is only a placeholder (set by pair.js); the field starts empty.
    assert 'placeholder="Cena (EUR)"' in html
    assert 'name="target_price" value=' not in html


def test_pair_page_has_tags_and_note_section(client):
    html = client.get("/pair/BTCEUR").text

    assert 'class="panel pair-meta" data-symbol="BTCEUR"' in html
    # The same "⋯" panel as in the markets table, hidden until /api/notes/config allows edits.
    assert 'id="pair-panel"' in html
    assert 'id="pair-tooltip"' in html
    assert 'class="row-menu note-button"' in html
    assert 'class="pair-note-text" hidden' in html


def test_pair_page_has_chart_notes_list(client):
    html = client.get("/pair/BTCEUR").text

    assert 'id="notes-panel-list"' in html


@pytest.mark.parametrize(
    ("symbol", "previous", "next_"),
    [("BTCEUR", "", "ETHEUR"), ("SOLEUR", "ETHEUR", "BNBEUR"), ("ADAEUR", "BNBEUR", "")],
)
def test_pair_page_knows_neighbouring_pairs(client, symbol, previous, next_):
    html = client.get(f"/pair/{symbol}").text

    assert f'data-previous="{previous}"' in html
    assert f'data-next="{next_}"' in html


def test_pair_page_loads_shared_scripts_then_its_own(client):
    html = client.get("/pair/BTCEUR").text

    format_script = html.index('<script src="/static/js/format.js"></script>')
    ws_script = html.index('<script src="/static/js/ws.js"></script>')
    chart_script = html.index('<script src="/static/js/chart.js"></script>')
    orderbook_script = html.index('<script src="/static/js/orderbook.js"></script>')
    htmx_script = html.index("htmx.org@")
    pairmeta_script = html.index('<script src="/static/js/pairmeta.js"></script>')
    pair_script = html.index('<script src="/static/js/pair.js"></script>')
    assert format_script < ws_script < chart_script < orderbook_script < pair_script
    assert htmx_script < pair_script
    assert pairmeta_script < pair_script
    assert "/static/js/app.js" not in html


def test_unknown_pair_page_has_no_live_parts(client):
    html = client.get("/pair/XRPEUR").text

    assert "<title>Par ne postoji · ChangeWatch</title>" in html
    assert 'id="connection-indicator"' not in html
    assert "<script" not in html


@pytest.mark.parametrize("spelling", ["btceur", "BTC-EUR", "btc-eur", "BtcEur"])
def test_pair_page_redirects_to_canonical_symbol(client, spelling):
    response = client.get(f"/pair/{spelling}", follow_redirects=False)

    assert response.status_code == 301
    assert response.headers["location"] == "http://testserver/pair/BTCEUR"


def test_pair_page_redirect_keeps_query(client):
    response = client.get("/pair/btceur?interval=5m", follow_redirects=False)

    assert response.status_code == 301
    assert response.headers["location"] == "http://testserver/pair/BTCEUR?interval=5m"


def test_unknown_pair_page_is_404_with_link_back(client):
    response = client.get("/pair/XRPEUR")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "← Svi parovi" in response.text
    assert 'href="/"' in response.text


def test_unknown_pair_page_escapes_symbol(client):
    response = client.get("/pair/%3Cscript%3E")

    assert response.status_code == 404
    assert "<script>" not in response.text


def test_pair_page_is_hidden_from_docs(client):
    paths = client.get("/openapi.json").json()["paths"]

    assert not any(path.startswith("/pair/") for path in paths)
    assert "/api/pairs/{symbol}/summary" in paths
