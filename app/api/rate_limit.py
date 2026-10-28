import math
import time
from collections import deque
from collections.abc import Callable

from fastapi import HTTPException, Request

# Above this many tracked IPs, addresses with no recent requests are dropped, so bots
# rotating through addresses cannot grow the dict forever.
SWEEP_THRESHOLD = 1000


class RateLimiter:
    """At most `max_requests` per IP in a sliding window, kept in memory.

    Enough for one process (SPEC 16.4); several instances would need a shared store
    such as Redis.
    """

    def __init__(
        self,
        max_requests: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.clock = clock
        self._requests: dict[str, deque[float]] = {}

    def __call__(self, request: Request) -> None:
        now = self.clock()
        if len(self._requests) > SWEEP_THRESHOLD:
            self._sweep(now)

        ip = request.client.host if request.client else "unknown"
        timestamps = self._requests.setdefault(ip, deque())
        while timestamps and timestamps[0] <= now - self.window_seconds:
            timestamps.popleft()

        if len(timestamps) >= self.max_requests:
            retry_after = math.ceil(timestamps[0] + self.window_seconds - now)
            raise HTTPException(
                status_code=429,
                detail="Previše zahteva, pokušaj ponovo kasnije",
                headers={"Retry-After": str(max(retry_after, 1))},
            )
        timestamps.append(now)

    def reset(self) -> None:
        self._requests.clear()

    def _sweep(self, now: float) -> None:
        cutoff = now - self.window_seconds
        for ip in [ip for ip, ts in self._requests.items() if not ts or ts[-1] <= cutoff]:
            del self._requests[ip]


# One shared budget for every POST, PATCH and DELETE route (SPEC 16.10).
write_limiter = RateLimiter(max_requests=30, window_seconds=60)
# Routes that call Binance REST on behalf of the browser; keeps one client from spending
# the server's Binance request weight.
history_limiter = RateLimiter(max_requests=20, window_seconds=60)
# CSV exports read up to 100000 rows each; keeps one client from tying up the database.
export_limiter = RateLimiter(max_requests=10, window_seconds=60)
