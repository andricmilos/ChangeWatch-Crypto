import asyncio
import contextlib
import json
import logging
import random
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from websockets.asyncio.client import connect

from app.config import get_settings
from app.schemas.market import KlineUpdate, MarketTicker

logger = logging.getLogger(__name__)

QUOTE_ASSETS = ("USDT", "USDC", "BTC", "ETH", "BNB")


def to_display_symbol(symbol: str) -> str:
    for quote in QUOTE_ASSETS:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return f"{symbol[: -len(quote)]}/{quote}"
    return symbol


def ms_to_datetime(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def _to_decimal(value: Any) -> Decimal | None:
    """Convert a Binance numeric string to Decimal; None when missing or not a finite number."""
    # Binance sends numbers as strings; refusing floats keeps binary rounding errors out.
    if not isinstance(value, str | int) or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    return number


def _to_positive_decimal(value: Any) -> Decimal | None:
    number = _to_decimal(value)
    if number is None or number <= 0:
        return None
    return number


def _to_datetime(value: Any) -> datetime | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    try:
        return ms_to_datetime(value)
    except (OverflowError, OSError, ValueError):
        return None


def parse_ticker(data: dict[str, Any], allowed: set[str] | None = None) -> MarketTicker | None:
    symbol = data.get("s")
    if not isinstance(symbol, str) or (allowed is not None and symbol not in allowed):
        logger.warning("Ignoring ticker for unsupported symbol: %r", symbol)
        return None

    last_price = _to_positive_decimal(data.get("c"))
    if last_price is None:
        logger.warning("Ignoring ticker for %s with invalid last price: %r", symbol, data.get("c"))
        return None

    event_time = _to_datetime(data.get("E"))
    if event_time is None:
        logger.warning("Ignoring ticker for %s with invalid event time: %r", symbol, data.get("E"))
        return None

    return MarketTicker(
        symbol=symbol,
        display_symbol=to_display_symbol(symbol),
        last_price=last_price,
        price_change=_to_decimal(data.get("p")),
        price_change_percent=_to_decimal(data.get("P")),
        high_24h=_to_positive_decimal(data.get("h")),
        low_24h=_to_positive_decimal(data.get("l")),
        volume_24h=_to_decimal(data.get("v")),
        event_time=event_time,
    )


def parse_kline(data: dict[str, Any], allowed: set[str] | None = None) -> KlineUpdate | None:
    kline = data.get("k")
    if not isinstance(kline, dict):
        logger.warning("Ignoring kline message without 'k' payload")
        return None

    symbol = kline.get("s", data.get("s"))
    if not isinstance(symbol, str) or (allowed is not None and symbol not in allowed):
        logger.warning("Ignoring kline for unsupported symbol: %r", symbol)
        return None

    interval = kline.get("i")
    open_time = _to_datetime(kline.get("t"))
    close_time = _to_datetime(kline.get("T"))
    prices = [_to_positive_decimal(kline.get(key)) for key in ("o", "h", "l", "c")]
    volume = _to_decimal(kline.get("v"))
    is_closed = kline.get("x")

    if (
        not isinstance(interval, str)
        or open_time is None
        or close_time is None
        or any(price is None for price in prices)
        or volume is None
        or volume < 0
        or not isinstance(is_closed, bool)
    ):
        logger.warning("Ignoring invalid kline for %s", symbol)
        return None

    open_price, high, low, close = prices
    return KlineUpdate(
        symbol=symbol,
        interval=interval,
        open_time=open_time,
        close_time=close_time,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
        is_closed=is_closed,
    )


def parse_message(raw: str, allowed: set[str]) -> MarketTicker | KlineUpdate | None:
    try:
        message = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Ignoring message that is not valid JSON")
        return None

    logger.debug("Raw exchange message: %s", raw)

    if not isinstance(message, dict):
        logger.warning("Ignoring message that is not a JSON object")
        return None

    # Combined streams wrap the payload as {"stream": ..., "data": {...}}.
    data = message.get("data", message)
    if not isinstance(data, dict):
        logger.warning("Ignoring message with invalid 'data' payload")
        return None

    event_type = data.get("e")
    if event_type == "24hrTicker":
        return parse_ticker(data, allowed)
    if event_type == "kline":
        return parse_kline(data, allowed)
    return None


def build_stream_url(base_url: str, symbols: list[str], intervals: list[str]) -> str:
    streams: list[str] = []
    for symbol in symbols:
        name = symbol.lower()
        streams.append(f"{name}@ticker")
        streams.extend(f"{name}@kline_{interval}" for interval in intervals)
    return f"{base_url.rstrip('/')}/stream?streams={'/'.join(streams)}"


def compute_backoff(attempt: int, initial: float, maximum: float) -> float:
    return min(initial * 2**attempt, maximum)


ClientStatus = Literal["connecting", "connected", "reconnecting", "stopped"]
TickerCallback = Callable[[MarketTicker], Awaitable[None]]
KlineCallback = Callable[[KlineUpdate], Awaitable[None]]
# Returns an async context manager yielding a connection with recv() and close();
# tests pass a fake one so no real network is used.
ConnectFactory = Callable[[str], Any]


class BinanceStreamClient:
    """One shared connection to Binance combined streams, with automatic reconnect."""

    def __init__(
        self,
        url: str,
        on_ticker: TickerCallback,
        on_kline: KlineCallback,
        connect_factory: ConnectFactory = connect,
        *,
        allowed_symbols: set[str],
        receive_timeout: float = 30,
        reconnect_initial_delay: float = 1.0,
        reconnect_max_delay: float = 60.0,
    ) -> None:
        self.url = url
        self.on_ticker = on_ticker
        self.on_kline = on_kline
        self.connect_factory = connect_factory
        self.allowed_symbols = allowed_symbols
        self.receive_timeout = receive_timeout
        self.reconnect_initial_delay = reconnect_initial_delay
        self.reconnect_max_delay = reconnect_max_delay

        self.status: ClientStatus = "stopped"
        self.last_message_at: datetime | None = None
        self._stop_event = asyncio.Event()
        self._connection: Any = None

    async def run(self) -> None:
        self._stop_event.clear()
        self.status = "connecting"
        attempt = 0
        while not self._stop_event.is_set():
            try:
                async with self.connect_factory(self.url) as connection:
                    self._connection = connection
                    self.status = "connected"
                    attempt = 0
                    logger.info("Connected to Binance stream")
                    await self._receive_loop(connection)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                logger.warning("Binance stream connection lost: %r", exc)
            finally:
                self._connection = None

            if self._stop_event.is_set():
                break
            self.status = "reconnecting"
            delay = compute_backoff(attempt, self.reconnect_initial_delay, self.reconnect_max_delay)
            # Jitter keeps many clients from reconnecting at exactly the same moment.
            delay += random.uniform(0, delay * 0.1)
            attempt += 1
            logger.info("Reconnecting to Binance in %.1f s (attempt %d)", delay, attempt)
            await self._sleep_unless_stopped(delay)

        self.status = "stopped"
        logger.info("Binance stream client stopped")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._connection is not None:
            await self._connection.close()

    async def _receive_loop(self, connection: Any) -> None:
        while not self._stop_event.is_set():
            try:
                raw = await asyncio.wait_for(connection.recv(), timeout=self.receive_timeout)
            except TimeoutError as exc:
                raise ConnectionError(
                    f"No message from Binance for {self.receive_timeout} s"
                ) from exc
            self.last_message_at = datetime.now(UTC)
            await self._dispatch(raw)
        # Treat a connection that ends without an error as a disconnect too.
        if not self._stop_event.is_set():
            raise ConnectionError("Binance stream closed")

    async def _dispatch(self, raw: str) -> None:
        event = parse_message(raw, self.allowed_symbols)
        try:
            if isinstance(event, MarketTicker):
                await self.on_ticker(event)
            elif isinstance(event, KlineUpdate):
                await self.on_kline(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failing consumer must not tear down the shared exchange connection.
            logger.exception("Error while handling Binance message")

    async def _sleep_unless_stopped(self, delay: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)


async def _print_live_prices(duration: int) -> None:
    settings = get_settings()
    symbols = ["BTCUSDT", "ETHUSDT"]

    async def print_ticker(ticker: MarketTicker) -> None:
        print(f"{ticker.event_time:%H:%M:%S} {ticker.display_symbol} {ticker.last_price}")

    async def ignore_kline(kline: KlineUpdate) -> None:
        return None

    client = BinanceStreamClient(
        build_stream_url(settings.binance_ws_url, symbols, []),
        on_ticker=print_ticker,
        on_kline=ignore_kline,
        allowed_symbols=set(symbols),
        receive_timeout=settings.receive_timeout_seconds,
        reconnect_initial_delay=settings.reconnect_initial_delay,
        reconnect_max_delay=settings.reconnect_max_delay,
    )
    task = asyncio.create_task(client.run())
    await asyncio.sleep(duration)
    await client.stop()
    await task


if __name__ == "__main__":
    # Temporary manual check: python -m app.services.exchange_client [seconds]
    # Prints live BTC and ETH prices for 20 seconds (or the given number), then stops.
    logging.basicConfig(level=get_settings().log_level.upper())
    asyncio.run(_print_live_prices(int(sys.argv[1]) if len(sys.argv) > 1 else 20))
