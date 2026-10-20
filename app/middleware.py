import logging
import time
from collections.abc import Callable

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

SLOW_REQUEST_MS = 500
# Polled by the deploy platform and fetched on every page load; logging them would bury
# the requests worth reading.
QUIET_PATHS = ("/health",)
QUIET_PREFIXES = ("/static/",)


class RequestTimingMiddleware:
    """Logs one line per HTTP request and adds a Server-Timing header to the response.

    Plain ASGI instead of BaseHTTPMiddleware: WebSocket scopes pass through untouched,
    and exceptions reach the app's error handlers unchanged.
    """

    def __init__(self, app: ASGIApp, clock: Callable[[], float] = time.perf_counter) -> None:
        self.app = app
        self.clock = clock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = self.clock()
        status = 500

        async def send_with_timing(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = MutableHeaders(scope=message)
                headers.append("Server-Timing", f"app;dur={self._elapsed_ms(start):.1f}")
            await send(message)

        try:
            await self.app(scope, receive, send_with_timing)
        except Exception:
            # Re-raised so the app's exception handler still builds the 500 response.
            log_request(scope, 500, self._elapsed_ms(start))
            raise
        log_request(scope, status, self._elapsed_ms(start))

    def _elapsed_ms(self, start: float) -> float:
        return (self.clock() - start) * 1000


def log_request(scope: Scope, status: int, duration_ms: float) -> None:
    # scope["path"] never contains the query string, which may carry user input.
    path = scope["path"]
    if path in QUIET_PATHS or path.startswith(QUIET_PREFIXES):
        return
    level = logging.WARNING if duration_ms > SLOW_REQUEST_MS else logging.INFO
    logger.log(level, "%s %s %d %.1fms", scope["method"], path, status, duration_ms)
