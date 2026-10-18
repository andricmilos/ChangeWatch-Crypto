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
