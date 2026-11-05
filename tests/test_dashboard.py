import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_market_service
from app.config import Settings
from app.main import app
from app.services.market_data_service import MarketDataService


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
def fake_service():
    # Never started, so there is no Binance connection and no prices at all.
    return MarketDataService(Settings(_env_file=None))


@pytest.fixture
def client(fake_service, lifespan_starts):
    original_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_market_service] = lambda: fake_service
    # No `with` block: the lifespan (and the real Binance client) does not start.
    yield TestClient(app)
    app.dependency_overrides.clear()
    app.dependency_overrides.update(original_overrides)
    assert lifespan_starts == []


def test_dashboard_lists_supported_pairs(client):
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    for display_symbol in ["BTC/EUR", "ETH/EUR", "SOL/EUR", "BNB/EUR", "ADA/EUR"]:
        assert display_symbol in html
    assert 'data-symbol="SOLEUR"' in html


def test_dashboard_starts_in_loading_state_without_fake_prices(client):
    html = client.get("/").text

    assert "Učitavanje cena…" in html
    assert "—" in html
    assert ">0<" not in html


def test_dashboard_loads_htmx_and_app_script(client):
    html = client.get("/").text

    assert "htmx.org@2" in html
    assert '<script src="/static/js/app.js"></script>' in html


def test_dashboard_has_calculator_panel(client):
    html = client.get("/").text

    assert "Kalkulator" in html
    for currency in ["EUR", "BTC", "ETH", "SOL", "BNB", "ADA"]:
        assert f'<option value="{currency}">{currency}</option>' in html
    assert 'id="converter-swap"' in html
    assert "Ne uključuje naknade i nije ponuda za" in html
    assert '<script src="/static/js/converter.js"></script>' in html


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/static/js/app.js", "javascript"),
        ("/static/js/chart.js", "javascript"),
        ("/static/js/converter.js", "javascript"),
        ("/static/js/format.js", "javascript"),
        ("/static/js/orderbook.js", "javascript"),
        ("/static/js/pair.js", "javascript"),
        ("/static/js/pairmeta.js", "javascript"),
        ("/static/js/ws.js", "javascript"),
        ("/static/css/style.css", "text/css"),
    ],
)
def test_static_files_are_served(client, path, content_type):
    response = client.get(path)

    assert response.status_code == 200
    assert content_type in response.headers["content-type"]


def test_dashboard_has_chart_panel(client):
    html = client.get("/").text

    assert 'id="chart"' in html
    for interval in ["1m", "5m", "15m", "1h"]:
        assert f'data-interval="{interval}"' in html
    assert 'data-chart-type="candles"' in html
    assert 'data-chart-type="line"' in html
    assert "Podaci nisu aktuelni" in html
    assert "UTC" in html


def test_chart_scripts_load_before_app_script(client):
    html = client.get("/").text

    library = html.index("lightweight-charts@5.2/dist/lightweight-charts.standalone.production.js")
    chart_script = html.index('<script src="/static/js/chart.js"></script>')
    app_script = html.index('<script src="/static/js/app.js"></script>')
    assert library < chart_script < app_script


def test_footer_has_tradingview_attribution(client):
    html = client.get("/").text
    footer = html[html.index("<footer") : html.index("</footer>")]

    assert 'href="https://www.tradingview.com/"' in footer
    assert "TradingView Lightweight Charts™" in footer
    assert "Copyright (c) 2025 TradingView, Inc." in footer


def test_dashboard_has_orderbook_panel(client):
    html = client.get("/").text
    panel = html[html.index('class="panel panel-orderbook"') : html.index("panel-watchlist")]

    assert "Knjiga naloga" in panel
    assert 'id="orderbook-toggle"' in panel
    assert "Učitavanje…" in panel
    assert "Zastarelo" in panel
    for column in ["Cena", "Količina", "Ukupno"]:
        assert f'<th class="num">{column}</th>' in panel
    asks = panel[panel.index('id="orderbook-asks"') : panel.index("orderbook-mid")]
    bids = panel[panel.index('id="orderbook-bids"') :]
    assert asks.count('class="orderbook-price"') == 10
    assert bids.count('class="orderbook-price"') == 10


def test_orderbook_script_loads_before_app_script(client):
    html = client.get("/").text

    orderbook_script = html.index('<script src="/static/js/orderbook.js"></script>')
    app_script = html.index('<script src="/static/js/app.js"></script>')
    assert orderbook_script < app_script


def test_dashboard_has_pair_tag_controls(client):
    html = client.get("/").text
    body = html[html.index('id="markets-body"') : html.index("</tbody>")]

    assert body.count('class="pair-name"') == body.count("<tr ")
    assert body.count('class="row-menu"') == body.count("<tr ")
    assert 'aria-label="Oznake i beleška za BTC/EUR"' in body
    for element_id in ["tag-filter", "tags-manage", "tags-manager", "pair-panel", "pair-tooltip"]:
        assert f'id="{element_id}"' in html
    assert 'id="pair-panel-note-count" class="note-count" aria-live="polite">0/500<' in html


def test_pairmeta_script_loads_before_app_script(client):
    html = client.get("/").text

    pairmeta_script = html.index('<script src="/static/js/pairmeta.js"></script>')
    app_script = html.index('<script src="/static/js/app.js"></script>')
    assert pairmeta_script < app_script


def test_shared_scripts_load_before_page_scripts(client):
    html = client.get("/").text

    format_script = html.index('<script src="/static/js/format.js"></script>')
    ws_script = html.index('<script src="/static/js/ws.js"></script>')
    chart_script = html.index('<script src="/static/js/chart.js"></script>')
    app_script = html.index('<script src="/static/js/app.js"></script>')
    converter_script = html.index('<script src="/static/js/converter.js"></script>')
    assert format_script < ws_script < chart_script < app_script < converter_script


def test_dashboard_keeps_connection_status_and_banners(client):
    html = client.get("/").text

    assert 'id="connection-indicator"' in html
    assert 'id="last-update"' in html
    assert 'id="banner-server"' in html
    assert 'id="banner-exchange"' in html
    assert 'id="favicon"' in html
    assert "<title>ChangeWatch Crypto</title>" in html


def test_dashboard_orderbook_explains_price_click(client):
    html = client.get("/").text

    assert "Klikni na cenu da je upišeš kao ciljnu cenu novog alerta." in html
