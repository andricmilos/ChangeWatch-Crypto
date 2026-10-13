from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"
    database_url: str = "sqlite+aiosqlite:///./data/changewatch.db"
    supported_symbols: list[str] = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "ADAUSDT"]
    binance_ws_url: str = "wss://data-stream.binance.vision"
    binance_rest_url: str = "https://data-api.binance.vision"
    stale_after_seconds: int = 15
    reconnect_initial_delay: float = 1.0
    reconnect_max_delay: float = 60.0
    kline_intervals: list[str] = ["1m", "5m", "15m", "1h"]
    receive_timeout_seconds: int = 30

    @field_validator("supported_symbols")
    @classmethod
    def normalize_symbols(cls, symbols: list[str]) -> list[str]:
        normalized = [symbol.strip().upper() for symbol in symbols]
        if not normalized:
            raise ValueError("supported_symbols must not be empty")
        return normalized


@lru_cache
def get_settings() -> Settings:
    return Settings()
