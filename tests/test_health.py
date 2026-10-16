import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_market_service
from app.config import Settings
from app.main import app
from app.services.market_data_service import MarketDataService


@pytest.fixture
def client():
    # Never started, so there is no Binance connection.
    service = MarketDataService(Settings(_env_file=None))
    original_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_market_service] = lambda: service
    # No `with` block: the lifespan (and the real Binance client) does not start.
    yield TestClient(app)
    app.dependency_overrides.clear()
    app.dependency_overrides.update(original_overrides)


def test_health_answers_200_without_exchange_connection(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "degraded",
        "exchange_connection": "stopped",
        "seconds_since_last_message": None,
    }
