import asyncio
import time
from collections.abc import Awaitable, Callable

from app.schemas.market import WindowStats

STATS_WINDOW = "7d"
DEFAULT_CACHE_SECONDS = 60.0

FetchWindowStats = Callable[[str, str], Awaitable[WindowStats]]


class PairStatsService:
    """7-day statistics per pair from Binance REST, cached in memory for a minute."""

    def __init__(
        self,
        fetch_window_stats: FetchWindowStats,
        cache_seconds: float = DEFAULT_CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.fetch_window_stats = fetch_window_stats
        self.cache_seconds = cache_seconds
        self.clock = clock
        # symbol -> (fetched_at, stats); only successful answers are kept.
        self._cache: dict[str, tuple[float, WindowStats]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get_7d(self, symbol: str) -> WindowStats:
        """Raises ExchangeUnavailableError when Binance cannot be reached."""
        # One request per pair at a time: page loads arriving together share the answer
        # instead of each spending Binance request weight.
        async with self._locks.setdefault(symbol, asyncio.Lock()):
            cached = self._cache.get(symbol)
            if cached is not None and self.clock() - cached[0] < self.cache_seconds:
                return cached[1]
            stats = await self.fetch_window_stats(symbol, STATS_WINDOW)
            self._cache[symbol] = (self.clock(), stats)
            return stats
