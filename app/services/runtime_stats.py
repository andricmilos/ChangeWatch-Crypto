"""In-memory counters behind GET /api/status (SPEC 16.12).

Services call the record_* methods; RuntimeStats knows nothing about Binance messages or
the database, it only counts. Every method is a few integer operations without I/O, so
calling it on every exchange message is cheap. Everything runs on one event loop, so no
locks are needed.
"""

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

WINDOW_SECONDS = 60
MAX_REASON_LENGTH = 200


def utc_now() -> datetime:
    return datetime.now(UTC)


class RecentCounter:
    """Events in the last `window` seconds, kept as one counter per second in a ring buffer.

    Memory stays at `window` slots no matter how many events arrive.
    """

    def __init__(self, window: int = WINDOW_SECONDS) -> None:
        self.window = window
        # Slot i holds the count for the second stored in _seconds[i]; an older second
        # in a slot means the slot is stale and starts over.
        self._seconds = [-1] * window
        self._counts = [0] * window

    def add(self, now: float) -> None:
        second = int(now)
        slot = second % self.window
        if self._seconds[slot] != second:
            self._seconds[slot] = second
            self._counts[slot] = 0
        self._counts[slot] += 1

    def total(self, now: float) -> int:
        oldest = int(now) - self.window
        return sum(
            count
            for second, count in zip(self._seconds, self._counts, strict=True)
            if second > oldest
        )


class RuntimeStats:
    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = utc_now,
    ) -> None:
        # clock measures durations and ages, wall_clock gives the times shown to people.
        self.clock = clock
        self.wall_clock = wall_clock
        self.started_at = wall_clock()
        self._started = clock()

        self.exchange_status = "stopped"
        self.connected_since: datetime | None = None
        self.reconnects = 0
        self.server_shutdowns = 0
        self.last_disconnect_at: datetime | None = None
        self.last_disconnect_reason: str | None = None

        self.messages_total = 0
        self.messages_rejected = 0
        self._recent_messages = RecentCounter()
        self._pair_message_at: dict[str, float] = {}

        self.browser_messages_sent = 0
        self.browser_send_failures = 0

        self.candles_written = 0
        self.db_write_errors = 0
        self.last_cleanup_at: datetime | None = None
        self.last_cleanup_result: str | None = None
        self.last_cleanup_deleted: int | None = None

        self.alerts_triggered = 0

    # ---------- Exchange connection ----------

    def record_exchange_status(self, status: str) -> None:
        if status == "connected":
            self.connected_since = self.wall_clock()
        elif self.exchange_status == "connected":
            self.connected_since = None
        self.exchange_status = status

    def record_disconnect(self, reason: str) -> None:
        self.last_disconnect_at = self.wall_clock()
        self.last_disconnect_reason = reason[:MAX_REASON_LENGTH]

    def record_reconnect_attempt(self) -> None:
        self.reconnects += 1

    def record_server_shutdown(self) -> None:
        self.server_shutdowns += 1

    # ---------- Exchange messages ----------

    def record_message(self) -> None:
        self.messages_total += 1
        self._recent_messages.add(self.clock())

    def record_rejected_message(self) -> None:
        self.messages_rejected += 1

    def record_pair_message(self, symbol: str) -> None:
        self._pair_message_at[symbol] = self.clock()

    def messages_last_60s(self) -> int:
        return self._recent_messages.total(self.clock())

    def seconds_since_pair_message(self, symbol: str) -> float | None:
        received_at = self._pair_message_at.get(symbol)
        if received_at is None:
            return None
        return round(self.clock() - received_at, 1)

    # ---------- Browsers, database, alerts ----------

    def record_browser_send(self, ok: bool) -> None:
        if ok:
            self.browser_messages_sent += 1
        else:
            self.browser_send_failures += 1

    def record_candles_written(self, count: int) -> None:
        self.candles_written += count

    def record_db_write_error(self) -> None:
        self.db_write_errors += 1

    def record_cleanup(self, deleted: int) -> None:
        self.last_cleanup_at = self.wall_clock()
        self.last_cleanup_result = "ok"
        self.last_cleanup_deleted = deleted

    def record_cleanup_failed(self) -> None:
        self.last_cleanup_at = self.wall_clock()
        self.last_cleanup_result = "error"
        self.last_cleanup_deleted = None

    def record_alert_triggered(self) -> None:
        self.alerts_triggered += 1

    # ---------- Report ----------

    def uptime_seconds(self) -> float:
        return round(self.clock() - self._started, 1)

    def snapshot(self) -> dict[str, Any]:
        """Counters only; live values (browsers, active alerts, freshness) come from services."""
        return {
            "started_at": self.started_at,
            "uptime_seconds": self.uptime_seconds(),
            "exchange": {
                "status": self.exchange_status,
                "connected_since": self.connected_since,
                "reconnects": self.reconnects,
                "server_shutdowns": self.server_shutdowns,
                "last_disconnect_at": self.last_disconnect_at,
                "last_disconnect_reason": self.last_disconnect_reason,
            },
            "messages": {
                "total": self.messages_total,
                "last_60s": self.messages_last_60s(),
                "rejected": self.messages_rejected,
            },
            "browsers": {
                "messages_sent": self.browser_messages_sent,
                "send_failures": self.browser_send_failures,
            },
            "database": {
                "candles_written": self.candles_written,
                "write_errors": self.db_write_errors,
                "last_cleanup_at": self.last_cleanup_at,
                "last_cleanup_result": self.last_cleanup_result,
                "last_cleanup_deleted": self.last_cleanup_deleted,
            },
            "alerts": {"triggered": self.alerts_triggered},
        }
