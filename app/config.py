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
    # While the stream has been down this long, prices are polled from REST at this interval.
    rest_fallback_after_seconds: int = 30
    rest_fallback_interval_seconds: int = 10
    # Spread (in % of the mid price) above which a market is flagged as less liquid.
    spread_warning_percent: Decimal = Decimal("0.5")
    # Stored 1m candles older than this are deleted by the periodic cleanup.
    candle_retention_days: int = 30
    # False makes chart notes read-only, e.g. for a public demo where anyone could edit them.
    notes_write_enabled: bool = True
    # Top bids and asks per pair from the <symbol>@depth<levels> stream (1000 ms updates).
    orderbook_enabled: bool = True
    orderbook_levels: int = 10
    # A pair's order book older than this is shown as stale; Binance sends one every second.
    orderbook_stale_seconds: int = 5

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

    @field_validator("rest_fallback_after_seconds", "rest_fallback_interval_seconds")
    @classmethod
    def check_rest_fallback_seconds(cls, seconds: int) -> int:
        # 0 would poll Binance in a tight loop and quickly hit its rate limit.
        if seconds <= 0:
            raise ValueError("REST fallback seconds must be greater than 0")
        return seconds

    @field_validator("candle_retention_days")
    @classmethod
    def check_candle_retention_days(cls, days: int) -> int:
        if days <= 0:
            raise ValueError("candle_retention_days must be greater than 0")
        return days

    @field_validator("orderbook_levels")
    @classmethod
    def check_orderbook_levels(cls, levels: int) -> int:
        # Binance only offers these partial book depths.
        if levels not in (5, 10, 20):
            raise ValueError("orderbook_levels must be 5, 10 or 20")
        return levels

    @field_validator("orderbook_stale_seconds")
    @classmethod
    def check_orderbook_stale_seconds(cls, seconds: int) -> int:
        if seconds <= 0:
            raise ValueError("orderbook_stale_seconds must be greater than 0")
        return seconds


@lru_cache
def get_settings() -> Settings:
    return Settings()
