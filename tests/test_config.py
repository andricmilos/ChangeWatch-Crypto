from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_default_values():
    settings = Settings(_env_file=None)
    assert settings.app_env == "development"
    assert settings.host == "127.0.0.1"
    assert settings.port == 8000
    assert settings.log_level == "INFO"
    assert settings.database_url == "sqlite+aiosqlite:///./data/changewatch.db"
    assert settings.supported_symbols == ["BTCEUR", "ETHEUR", "SOLEUR", "BNBEUR", "ADAEUR"]
    assert settings.binance_ws_url == "wss://data-stream.binance.vision"
    assert settings.binance_rest_url == "https://data-api.binance.vision"
    assert settings.stale_after_seconds == 15
    assert settings.reconnect_initial_delay == 1.0
    assert settings.reconnect_max_delay == 60.0
    assert settings.kline_intervals == ["1m", "5m", "15m", "1h"]
    assert settings.receive_timeout_seconds == 30
    assert settings.rolling_windows == ["1h", "4h"]
    assert settings.spread_warning_percent == Decimal("0.5")
    assert settings.candle_retention_days == 30


def test_spread_warning_percent_from_env(monkeypatch):
    monkeypatch.setenv("SPREAD_WARNING_PERCENT", "1.25")
    assert Settings(_env_file=None).spread_warning_percent == Decimal("1.25")


def test_symbols_are_uppercased_and_stripped():
    settings = Settings(_env_file=None, supported_symbols=["btceur ", " ethEUR"])
    assert settings.supported_symbols == ["BTCEUR", "ETHEUR"]


def test_empty_symbol_list_raises_error():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, supported_symbols=[])


def test_unsupported_rolling_window_raises_error():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, rolling_windows=["1d"])


def test_event_queue_sizes_default_and_come_from_env(monkeypatch):
    assert Settings(_env_file=None).event_queue_size_latest == 100
    monkeypatch.setenv("EVENT_QUEUE_SIZE_KEEP_ALL", "50")
    assert Settings(_env_file=None).event_queue_size_keep_all == 50


@pytest.mark.parametrize("field", ["event_queue_size_latest", "event_queue_size_keep_all"])
@pytest.mark.parametrize("size", [0, -1])
def test_event_queue_size_must_be_positive(field, size):
    with pytest.raises(ValidationError, match="event queue sizes must be greater than 0"):
        Settings(_env_file=None, **{field: size})
