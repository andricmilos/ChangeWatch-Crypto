"""Shape of GET /api/status, built from RuntimeStats.snapshot() plus live service values."""

from datetime import datetime

from pydantic import BaseModel, Field


class ExchangeStatus(BaseModel):
    status: str = Field(
        description="connecting, connected, reconnecting or stopped.", examples=["connected"]
    )
    connected_since: datetime | None = Field(
        description="Start of the current connection; null when not connected.",
        examples=["2026-10-21T12:00:03+00:00"],
    )
    reconnects: int = Field(description="Reconnect attempts since start.", examples=[2])
    server_shutdowns: int = Field(
        description="serverShutdown notices received from Binance.", examples=[0]
    )
    last_disconnect_at: datetime | None = Field(
        description="When the connection last dropped.", examples=["2026-10-21T11:58:40+00:00"]
    )
    last_disconnect_reason: str | None = Field(
        description="Why it dropped (at most 200 characters).", examples=["ConnectionClosedError"]
    )


class MessageCounts(BaseModel):
    total: int = Field(description="Exchange messages since start.", examples=[184230])
    last_60s: int = Field(description="Exchange messages in the last 60 seconds.", examples=[312])
    rejected: int = Field(description="Messages that failed validation.", examples=[0])


class PairStatus(BaseModel):
    symbol: str = Field(description="Pair in Binance format.", examples=["BTCEUR"])
    seconds_since_last_message: float | None = Field(
        description="Age of the last message for this pair; null if none yet.", examples=[0.8]
    )
    freshness: str = Field(description="live, stale or no_data.", examples=["live"])


class BrowserStats(BaseModel):
    connected: int = Field(description="Browsers connected to /ws/market now.", examples=[3])
    messages_sent: int = Field(description="Messages sent to browsers.", examples=[90211])
    send_failures: int = Field(description="Sends that failed.", examples=[0])


class DatabaseStats(BaseModel):
    candles_written: int = Field(description="Closed 1m candles stored.", examples=[742])
    write_errors: int = Field(description="Failed database writes.", examples=[0])
    last_cleanup_at: datetime | None = Field(
        description="When old candles were last cleaned up; null before the first run.",
        examples=["2026-10-21T12:00:01+00:00"],
    )
    last_cleanup_result: str | None = Field(
        description="ok or error; null before the first run.", examples=["ok"]
    )
    last_cleanup_deleted: int | None = Field(
        description="Candles deleted by the last cleanup; null if it failed.", examples=[1440]
    )


class AlertStats(BaseModel):
    active: int = Field(description="Alerts currently armed.", examples=[4])
    triggered: int = Field(description="Alerts fired since start.", examples=[1])


class StatusResponse(BaseModel):
    started_at: datetime = Field(
        description="When the server started.", examples=["2026-10-21T12:00:00+00:00"]
    )
    uptime_seconds: float = Field(description="Seconds since start.", examples=[7512.4])
    exchange: ExchangeStatus
    messages: MessageCounts
    markets: list[PairStatus]
    browsers: BrowserStats
    database: DatabaseStats
    alerts: AlertStats
