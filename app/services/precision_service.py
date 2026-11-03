import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.schemas.market import MarketMeta
from app.services.exchange_client import ExchangeUnavailableError, SymbolPrecision

logger = logging.getLogger(__name__)

FetchPrecision = Callable[[], Awaitable[dict[str, SymbolPrecision]]]


class PrecisionService:
    """Price and quantity decimals per pair from Binance exchangeInfo; null until loaded."""

    RETRY_DELAY_SECONDS = 60.0
    MAX_RETRIES = 5

    def __init__(
        self,
        fetch_precision: FetchPrecision,
        symbols: list[str],
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.fetch_precision = fetch_precision
        self.symbols = symbols
        self.sleep = sleep
        self._precision: dict[str, SymbolPrecision] = {}

    async def run(self) -> None:
        """Background task: one load at startup, retried a few times if Binance fails."""
        for retry in range(self.MAX_RETRIES + 1):
            try:
                self._precision = await self.fetch_precision()
            except ExchangeUnavailableError as exc:
                if retry == self.MAX_RETRIES:
                    # No made-up defaults: the browser keeps its own formatting instead.
                    logger.warning("Could not load pair precision from Binance, giving up: %s", exc)
                    return
                logger.warning(
                    "Could not load pair precision from Binance (%s); retry %d/%d in %.0f s",
                    exc,
                    retry + 1,
                    self.MAX_RETRIES,
                    self.RETRY_DELAY_SECONDS,
                )
                await self.sleep(self.RETRY_DELAY_SECONDS)
                continue
            missing = [symbol for symbol in self.symbols if symbol not in self._precision]
            if missing:
                logger.warning("Binance exchangeInfo has no entry for %s", ", ".join(missing))
            logger.info("Loaded pair precision for %d pairs", len(self._precision))
            return

    def view(self) -> list[MarketMeta]:
        """Every supported pair in configured order; decimals stay null until loaded."""
        unknown = SymbolPrecision(price_decimals=None, quantity_decimals=None)
        metas = []
        for symbol in self.symbols:
            precision = self._precision.get(symbol, unknown)
            metas.append(
                MarketMeta(
                    symbol=symbol,
                    price_decimals=precision.price_decimals,
                    quantity_decimals=precision.quantity_decimals,
                )
            )
        return metas
