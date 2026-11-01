import logging

import pytest

import app.main as main
from app.config import Settings, settings_summary


class SettingsWithSecret(Settings):
    api_key: str = "real-api-key-value"


class StopStartup(Exception):
    pass


def test_summary_contains_pairs_and_app_env():
    settings = Settings(_env_file=None, app_env="production", supported_symbols=["BTCEUR"])

    summary = settings_summary(settings, "1.2.3")

    assert "1.2.3" in summary
    assert "APP_ENV = production" in summary
    assert "SUPPORTED_SYMBOLS = ['BTCEUR']" in summary
    assert "NOTES_WRITE_ENABLED = True" in summary


def test_summary_hides_secret_fields_and_url_passwords():
    settings = SettingsWithSecret(
        _env_file=None, database_url="postgresql+asyncpg://admin:hunter2@db:5432/changewatch"
    )

    summary = settings_summary(settings)

    assert "API_KEY = ***" in summary
    assert "real-api-key-value" not in summary
    assert "DATABASE_URL = postgresql+asyncpg://admin:***@db:5432/changewatch" in summary
    assert "hunter2" not in summary


async def test_lifespan_logs_summary_once(monkeypatch, caplog):
    settings = Settings(_env_file=None, supported_symbols=["ETHEUR"])
    monkeypatch.setattr(main, "get_settings", lambda: settings)

    # Stops the startup right after the summary, so no database or Binance connection opens.
    async def stop_startup() -> None:
        raise StopStartup

    monkeypatch.setattr(main, "init_db", stop_startup)

    with caplog.at_level(logging.INFO, logger=main.logger.name), pytest.raises(StopStartup):
        async with main.lifespan(main.app):
            pass

    summaries = [r for r in caplog.records if "with settings:" in r.getMessage()]
    assert len(summaries) == 1
    assert summaries[0].levelno == logging.INFO
    assert "ETHEUR" in summaries[0].getMessage()
