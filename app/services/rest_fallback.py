import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from app.schemas.market import MarketTicker
from app.services.exchange_client import ExchangeUnavailableError
from app.services.market_data_service import MarketDataService

logger = logging.getLogger(__name__)

FetchTickers = Callable[[], Awaitable[list[MarketTicker]]]


class RestFallbackPoller:
    """Polls Binance REST for tickers while the WebSocket stream has been down for a while."""

    # How often the stream status is checked; also the precision of both thresholds.
    CHECK_INTERVAL_SECONDS = 1.0

    def __init__(
        self,
        service: MarketDataService,
        fetch_tickers: FetchTickers,
        *,
        after_seconds: float,
        interval_seconds: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.service = service
        self.fetch_tickers = fetch_tickers
        self.after_seconds = after_seconds
        self.interval_seconds = interval_seconds
        self.sleep = sleep
        self.clock = clock
        self.active = False
        self._down_since: float | None = None
        self._next_poll_at = 0.0

    async def run(self) -> None:
        """Background task; runs until cancelled."""
        while True:
            await self.check()
            await self.sleep(self.CHECK_INTERVAL_SECONDS)

    async def check(self) -> None:
        now = self.clock()
        if self.service.client.status == "connected":
            if self.active:
                logger.info("Binance stream is back; stopping REST price fallback")
            self.active = False
            self._down_since = None
            return

        if self._down_since is None:
            self._down_since = now
        if now - self._down_since < self.after_seconds or now < self._next_poll_at:
            return
        if not self.active:
            logger.warning(
                "Binance stream down for over %.0f s; polling REST prices every %.0f s",
                self.after_seconds,
                self.interval_seconds,
            )
            self.active = True
        self._next_poll_at = now + self.interval_seconds
        await self._poll()

    async def _poll(self) -> None:
        try:
            tickers = await self.fetch_tickers()
        except ExchangeUnavailableError as exc:
            # Rate limit (429/418) or network error: skip this round, try again next interval.
            logger.warning("REST price fallback round skipped: %s", exc)
            return
        await self.service.handle_rest_tickers(tickers)
