from decimal import Decimal
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    database_url: str = "sqlite+aiosqlite:///./data/changewatch.db"
    supported_symbols: list[str] = ["BTCEUR", "ETHEUR", "SOLEUR", "BNBEUR", "ADAEUR"]
    binance_ws_url: str = "wss://data-stream.binance.vision"
    binance_rest_url: str = "https://data-api.binance.vision"
    stale_after_seconds: int = 15
    reconnect_initial_delay: float = 1.0
    reconnect_max_delay: float = 60.0
    kline_intervals: list[str] = ["1m", "5m", "15m", "1h"]
    receive_timeout_seconds: int = 30
    rolling_windows: list[str] = ["1h", "4h"]
    # Spread (in % of the mid price) above which a market is flagged as less liquid.
    spread_warning_percent: Decimal = Decimal("0.5")

    @field_validator("supported_symbols")
    @classmethod
    def normalize_symbols(cls, symbols: list[str]) -> list[str]:
        normalized = [symbol.strip().upper() for symbol in symbols]
        if not normalized:
            raise ValueError("supported_symbols must not be empty")
        return normalized

    @field_validator("rolling_windows")
    @classmethod
    def check_rolling_windows(cls, windows: list[str]) -> list[str]:
        # Each window needs its own MarketView field, so only these two are supported.
        unknown = [window for window in windows if window not in ("1h", "4h")]
        if unknown:
            raise ValueError(f"Unsupported rolling windows {unknown}; use 1h and/or 4h")
        return windows


@lru_cache
def get_settings() -> Settings:
    return Settings()
