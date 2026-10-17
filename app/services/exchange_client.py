import asyncio
import contextlib
import json
import logging
import random
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

import httpx
from websockets.asyncio.client import connect

from app.config import Settings, get_settings
from app.schemas.market import KlineUpdate, MarketTicker
from app.services.runtime_stats import RuntimeStats

logger = logging.getLogger(__name__)

# Quote currencies are recognised by name, not by length: "EUR" has 3 letters, "USDT" 4.
QUOTE_ASSETS = ("EUR", "USDT", "USDC", "BTC", "ETH", "BNB")


def quote_asset(symbol: str) -> str | None:
    """Return the quote currency of a pair ("BTCEUR" -> "EUR"), or None if it is unknown."""
    for quote in QUOTE_ASSETS:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return quote
    return None


def to_display_symbol(symbol: str) -> str:
    quote = quote_asset(symbol)
    if quote is None:
        return symbol
    return f"{symbol[: -len(quote)]}/{quote}"


EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def ms_to_datetime(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def datetime_to_ms(value: datetime) -> int:
    # Integer timedelta division instead of timestamp() * 1000, which can round 999 down to 998.
    return (value - EPOCH) // timedelta(milliseconds=1)


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


class ExchangeUnavailableError(Exception):
    """Binance REST could not give a usable answer (network error, 5xx or rate limit)."""


def parse_rest_kline(
    row: Any, symbol: str, interval: str, is_closed: bool
) -> KlineUpdate | None:
    """Convert one /api/v3/klines row; the first seven fields are t, o, h, l, c, v, T."""
    if not isinstance(row, list) or len(row) < 7:
        logger.warning("Ignoring invalid REST kline row for %s", symbol)
        return None

    open_time = _to_datetime(row[0])
    close_time = _to_datetime(row[6])
    prices = [_to_positive_decimal(value) for value in row[1:5]]
    volume = _to_decimal(row[5])
    if (
        open_time is None
        or close_time is None
        or any(price is None for price in prices)
        or volume is None
        or volume < 0
    ):
        logger.warning("Ignoring invalid REST kline row for %s", symbol)
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


class BinanceRestClient:
    """Historical candles from Binance REST (public market data endpoint only)."""

    RATE_LIMIT_STATUSES = (418, 429)
    DEFAULT_RATE_LIMIT_PAUSE = 60.0

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        # Tests pass an httpx.MockTransport, so no request ever leaves the machine.
        self.http = httpx.AsyncClient(
            base_url=settings.binance_rest_url, timeout=10, transport=transport
        )
        self._paused_until = 0.0

    async def close(self) -> None:
        await self.http.aclose()

    async def get_klines(self, symbol: str, interval: str, limit: int) -> list[KlineUpdate]:
        if time.monotonic() < self._paused_until:
            # After 429/418 Binance bans clients that keep retrying, so wait out the pause.
            raise ExchangeUnavailableError("Binance rate limit pause is still active")

        try:
            response = await self.http.get(
                "/api/v3/klines", params={"symbol": symbol, "interval": interval, "limit": limit}
            )
        except httpx.HTTPError as exc:
            raise ExchangeUnavailableError(f"Binance REST request failed: {exc!r}") from exc

        if response.status_code in self.RATE_LIMIT_STATUSES:
            pause = self._retry_after(response)
            self._paused_until = time.monotonic() + pause
            logger.warning(
                "Binance REST rate limit hit (HTTP %d); pausing requests for %.0f s",
                response.status_code,
                pause,
            )
            raise ExchangeUnavailableError(f"Binance rate limit (HTTP {response.status_code})")
        if response.status_code >= 500:
            raise ExchangeUnavailableError(f"Binance REST returned HTTP {response.status_code}")
        if response.status_code != 200:
            logger.error(
                "Unexpected Binance REST response HTTP %d: %s",
                response.status_code,
                response.text[:200],
            )
            raise ExchangeUnavailableError(f"Binance REST returned HTTP {response.status_code}")

        try:
            rows = response.json()
        except ValueError as exc:
            raise ExchangeUnavailableError("Binance REST returned invalid JSON") from exc
        if not isinstance(rows, list):
            raise ExchangeUnavailableError("Binance REST returned an unexpected payload")

        last_index = len(rows) - 1
        # The newest candle is still being built, every earlier one is final.
        candles = [
            parse_rest_kline(row, symbol, interval, is_closed=index != last_index)
            for index, row in enumerate(rows)
        ]
        return [candle for candle in candles if candle is not None]

    def _retry_after(self, response: httpx.Response) -> float:
        try:
            return max(float(response.headers["Retry-After"]), 1.0)
        except (KeyError, ValueError):
            return self.DEFAULT_RATE_LIMIT_PAUSE


ClientStatus = Literal["connecting", "connected", "reconnecting", "stopped"]
TickerCallback = Callable[[MarketTicker], Awaitable[None]]
KlineCallback = Callable[[KlineUpdate], Awaitable[None]]
StatusCallback = Callable[[ClientStatus], Awaitable[None]]
# Returns an async context manager yielding a connection with recv() and close();
# tests pass a fake one so no real network is used.
ConnectFactory = Callable[[str], Any]


def connect_to_binance(url: str) -> connect:
    # Binance answers the close frame quickly but drops TCP only after ~5 s; the default
    # 10 s close_timeout would make every shutdown wait for that.
    return connect(url, close_timeout=1)


class BinanceStreamClient:
    """One shared connection to Binance combined streams, with automatic reconnect."""

    def __init__(
        self,
        url: str,
        on_ticker: TickerCallback,
        on_kline: KlineCallback,
        connect_factory: ConnectFactory = connect_to_binance,
        *,
        allowed_symbols: set[str],
        receive_timeout: float = 30,
        reconnect_initial_delay: float = 1.0,
        reconnect_max_delay: float = 60.0,
        on_status_change: StatusCallback | None = None,
        stats: RuntimeStats | None = None,
    ) -> None:
        self.url = url
        self.on_ticker = on_ticker
        self.on_kline = on_kline
        self.on_status_change = on_status_change
        self.connect_factory = connect_factory
        self.allowed_symbols = allowed_symbols
        self.receive_timeout = receive_timeout
        self.reconnect_initial_delay = reconnect_initial_delay
        self.reconnect_max_delay = reconnect_max_delay
        self.stats = stats or RuntimeStats()

        self.status: ClientStatus = "stopped"
        self.last_message_at: datetime | None = None
        self._stop_event = asyncio.Event()
        self._connection: Any = None

    async def run(self) -> None:
        self._stop_event.clear()
        await self._set_status("connecting")
        attempt = 0
        while not self._stop_event.is_set():
            reason = "connection closed"
            try:
                async with self.connect_factory(self.url) as connection:
                    self._connection = connection
                    await self._set_status("connected")
                    logger.info(
                        "Connected to Binance stream after %d failed attempt(s)", attempt
                    )
                    attempt = 0
                    await self._receive_loop(connection)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                reason = f"{type(exc).__name__}: {exc}"
            finally:
                self._connection = None

            if self._stop_event.is_set():
                break
            self.stats.record_disconnect(reason)
            await self._set_status("reconnecting")
            delay = compute_backoff(attempt, self.reconnect_initial_delay, self.reconnect_max_delay)
            # Jitter keeps many clients from reconnecting at exactly the same moment.
            delay += random.uniform(0, delay * 0.1)
            attempt += 1
            self.stats.record_reconnect_attempt()
            logger.warning(
                "Binance stream connection lost (%s); reconnect attempt %d in %.1f s",
                reason,
                attempt,
                delay,
            )
            await self._sleep_unless_stopped(delay)

        await self._set_status("stopped")
        logger.info("Binance stream client stopped")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._connection is not None:
            await self._connection.close()

    async def _set_status(self, status: ClientStatus) -> None:
        if status == self.status:
            return
        self.status = status
        self.stats.record_exchange_status(status)
        if self.on_status_change is None:
            return
        try:
            await self.on_status_change(status)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failing listener must not break the connect/reconnect loop.
            logger.exception("Error while handling Binance status change")

    async def _receive_loop(self, connection: Any) -> None:
        # stop() was called while connecting; leaving the `async with` closes the connection.
        if self._stop_event.is_set():
            return
        # Read until recv() raises, even after stop(): Binance's close frame arrives behind
        # already queued messages, so close() would hang until its timeout if we stopped reading.
        while True:
            try:
                raw = await asyncio.wait_for(connection.recv(), timeout=self.receive_timeout)
            except TimeoutError as exc:
                raise ConnectionError(
                    f"No message from Binance for {self.receive_timeout} s"
                ) from exc
            if self._stop_event.is_set():
                continue
            self.last_message_at = datetime.now(UTC)
            self.stats.record_message()
            await self._dispatch(raw)

    async def _dispatch(self, raw: str) -> None:
        event = parse_message(raw, self.allowed_symbols)
        if event is None:
            # Invalid JSON, a failed check (logged by the parser) or an unknown event type.
            self.stats.record_rejected_message()
            return
        self.stats.record_pair_message(event.symbol)
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
    symbols = ["BTCEUR", "ETHEUR"]

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
