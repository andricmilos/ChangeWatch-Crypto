import asyncio
import contextlib
import json
import logging
import random
import re
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal

import httpx
from websockets.asyncio.client import connect

from app.config import Settings, get_settings
from app.schemas.market import KlineUpdate, MarketTicker, RollingChange
from app.schemas.orderbook import OrderBookLevel, OrderBookSnapshot
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


SPREAD_PERCENT_STEP = Decimal("0.001")


def _parse_bid_ask(
    data: dict[str, Any], symbol: str, bid_key: str = "b", ask_key: str = "a"
) -> tuple[Decimal | None, Decimal | None]:
    """Best bid and ask; both None when either is unusable or the book is crossed."""
    bid = _to_positive_decimal(data.get(bid_key))
    ask = _to_positive_decimal(data.get(ask_key))
    if bid is None or ask is None:
        logger.warning(
            "Dropping bid/ask for %s: invalid bid %r or ask %r",
            symbol,
            data.get(bid_key),
            data.get(ask_key),
        )
        return None, None
    if ask < bid:
        logger.warning("Dropping bid/ask for %s: ask %s is below bid %s", symbol, ask, bid)
        return None, None
    return bid, ask


def _spread(bid: Decimal | None, ask: Decimal | None) -> tuple[Decimal | None, Decimal | None]:
    """Absolute and percent spread; both None without a usable book."""
    if bid is None or ask is None:
        return None, None
    return ask - bid, spread_percent(bid, ask)


def spread_percent(bid: Decimal, ask: Decimal) -> Decimal:
    """(ask - bid) relative to the mid price, in percent, rounded to 3 decimals."""
    mid = (ask + bid) / 2
    return ((ask - bid) / mid * 100).quantize(SPREAD_PERCENT_STEP, rounding=ROUND_HALF_UP)


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

    # A bad bid/ask only drops the book prices; the last price and 24h data stay usable.
    bid_price, ask_price = _parse_bid_ask(data, symbol)
    spread, spread_pct = _spread(bid_price, ask_price)
    return MarketTicker(
        symbol=symbol,
        display_symbol=to_display_symbol(symbol),
        last_price=last_price,
        price_change=_to_decimal(data.get("p")),
        price_change_percent=_to_decimal(data.get("P")),
        high_24h=_to_positive_decimal(data.get("h")),
        low_24h=_to_positive_decimal(data.get("l")),
        volume_24h=_to_decimal(data.get("v")),
        bid_price=bid_price,
        ask_price=ask_price,
        spread=spread,
        spread_percent=spread_pct,
        event_time=event_time,
    )


def parse_rest_ticker(data: Any, allowed: set[str] | None = None) -> MarketTicker | None:
    """One /api/v3/ticker/24hr entry, checked by the same rules as the WebSocket ticker."""
    if not isinstance(data, dict):
        logger.warning("Ignoring REST ticker that is not a JSON object")
        return None

    symbol = data.get("symbol")
    if not isinstance(symbol, str) or (allowed is not None and symbol not in allowed):
        logger.warning("Ignoring REST ticker for unsupported symbol: %r", symbol)
        return None

    last_price = _to_positive_decimal(data.get("lastPrice"))
    if last_price is None:
        logger.warning(
            "Ignoring REST ticker for %s with invalid last price: %r", symbol, data.get("lastPrice")
        )
        return None

    # closeTime ends the 24h window the statistics cover, so it plays the role of "E".
    event_time = _to_datetime(data.get("closeTime"))
    if event_time is None:
        logger.warning(
            "Ignoring REST ticker for %s with invalid close time: %r", symbol, data.get("closeTime")
        )
        return None

    bid_price, ask_price = _parse_bid_ask(data, symbol, "bidPrice", "askPrice")
    spread, spread_pct = _spread(bid_price, ask_price)
    return MarketTicker(
        symbol=symbol,
        display_symbol=to_display_symbol(symbol),
        last_price=last_price,
        price_change=_to_decimal(data.get("priceChange")),
        price_change_percent=_to_decimal(data.get("priceChangePercent")),
        high_24h=_to_positive_decimal(data.get("highPrice")),
        low_24h=_to_positive_decimal(data.get("lowPrice")),
        volume_24h=_to_decimal(data.get("volume")),
        bid_price=bid_price,
        ask_price=ask_price,
        spread=spread,
        spread_percent=spread_pct,
        event_time=event_time,
        source="rest",
    )


# Rolling window event type -> window; Binance also has "1dTicker", which is not used here.
ROLLING_EVENT_WINDOWS = {"1hTicker": "1h", "4hTicker": "4h"}


def parse_rolling_ticker(
    data: dict[str, Any], allowed: set[str] | None = None
) -> RollingChange | None:
    """Keep only symbol, window, percent change and time; the rest overlaps the 24h ticker."""
    # A non-string "e" (list, object) is unhashable and would crash the dict lookup.
    event_type = data.get("e")
    window = ROLLING_EVENT_WINDOWS.get(event_type) if isinstance(event_type, str) else None
    if window is None:
        logger.warning("Ignoring rolling ticker with unknown window: %r", data.get("e"))
        return None

    symbol = data.get("s")
    if not isinstance(symbol, str) or (allowed is not None and symbol not in allowed):
        logger.warning("Ignoring rolling ticker for unsupported symbol: %r", symbol)
        return None

    change_percent = _to_decimal(data.get("P"))
    if change_percent is None:
        logger.warning(
            "Ignoring %s ticker for %s with invalid change: %r", window, symbol, data.get("P")
        )
        return None

    event_time = _to_datetime(data.get("E"))
    if event_time is None:
        logger.warning(
            "Ignoring %s ticker for %s with invalid event time: %r", window, symbol, data.get("E")
        )
        return None

    return RollingChange(
        symbol=symbol, window=window, change_percent=change_percent, event_time=event_time
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


IMBALANCE_PERCENT_STEP = Decimal("0.01")
# "btceur@depth10"; the optional speed suffix is accepted so a renamed stream still parses.
DEPTH_STREAM = re.compile(r"^(?P<symbol>[a-z0-9]+)@depth(?:5|10|20)(?:@\d+ms)?$")


def is_depth_stream(stream: Any) -> bool:
    return isinstance(stream, str) and "@depth" in stream


def _parse_depth_side(levels: Any, symbol: str, side: str) -> list[tuple[Decimal, Decimal]] | None:
    """[["price", "quantity"], ...] as Decimals; None (with a warning) when anything is off."""
    if not isinstance(levels, list) or not levels:
        logger.warning("Dropping order book for %s: missing or empty %s", symbol, side)
        return None
    parsed: list[tuple[Decimal, Decimal]] = []
    for level in levels:
        if not isinstance(level, list) or len(level) < 2:
            logger.warning("Dropping order book for %s: invalid %s level %r", symbol, side, level)
            return None
        price = _to_decimal(level[0])
        quantity = _to_decimal(level[1])
        if price is None or quantity is None:
            logger.warning(
                "Dropping order book for %s: non-numeric %s level %r", symbol, side, level
            )
            return None
        if price <= 0 or quantity < 0:
            logger.warning(
                "Dropping order book for %s: %s level %r has price <= 0 or quantity < 0",
                symbol,
                side,
                level,
            )
            return None
        parsed.append((price, quantity))
    return parsed


def _is_strictly_ordered(prices: list[Decimal], descending: bool) -> bool:
    pairs = zip(prices, prices[1:], strict=False)
    if descending:
        return all(first > second for first, second in pairs)
    return all(first < second for first, second in pairs)


def _with_cumulative(levels: list[tuple[Decimal, Decimal]]) -> list[OrderBookLevel]:
    result: list[OrderBookLevel] = []
    total = Decimal(0)
    for price, quantity in levels:
        total += quantity
        result.append(OrderBookLevel(price=price, quantity=quantity, cumulative_quantity=total))
    return result


def imbalance_percent(bid_volume: Decimal, ask_volume: Decimal) -> Decimal | None:
    total = bid_volume + ask_volume
    if total == 0:
        return None
    return ((bid_volume - ask_volume) / total * 100).quantize(
        IMBALANCE_PERCENT_STEP, rounding=ROUND_HALF_UP
    )


def parse_depth(
    stream: str, data: dict[str, Any], allowed: set[str] | None, received_at: datetime
) -> OrderBookSnapshot | None:
    """A Partial Book Depth message; the payload carries no symbol, so it comes from `stream`."""
    match = DEPTH_STREAM.match(stream)
    symbol = match["symbol"].upper() if match else None
    if symbol is None or (allowed is not None and symbol not in allowed):
        logger.warning("Ignoring order book for unsupported stream: %r", stream)
        return None

    last_update_id = data.get("lastUpdateId")
    if not isinstance(last_update_id, int) or isinstance(last_update_id, bool):
        logger.warning(
            "Dropping order book for %s: invalid lastUpdateId %r", symbol, last_update_id
        )
        return None

    bids = _parse_depth_side(data.get("bids"), symbol, "bids")
    asks = _parse_depth_side(data.get("asks"), symbol, "asks")
    if bids is None or asks is None:
        return None
    if not _is_strictly_ordered([price for price, _ in bids], descending=True):
        logger.warning("Dropping order book for %s: bids are not in descending order", symbol)
        return None
    if not _is_strictly_ordered([price for price, _ in asks], descending=False):
        logger.warning("Dropping order book for %s: asks are not in ascending order", symbol)
        return None

    best_bid, best_ask = bids[0][0], asks[0][0]
    if best_bid >= best_ask:
        logger.warning(
            "Dropping order book for %s: best bid %s is not below best ask %s",
            symbol,
            best_bid,
            best_ask,
        )
        return None

    bid_levels = _with_cumulative(bids)
    ask_levels = _with_cumulative(asks)
    bid_volume = bid_levels[-1].cumulative_quantity
    ask_volume = ask_levels[-1].cumulative_quantity
    return OrderBookSnapshot(
        symbol=symbol,
        last_update_id=last_update_id,
        received_at=received_at,
        bids=bid_levels,
        asks=ask_levels,
        best_bid=best_bid,
        best_ask=best_ask,
        spread=best_ask - best_bid,
        spread_percent=spread_percent(best_bid, best_ask),
        mid_price=(best_bid + best_ask) / 2,
        bid_volume=bid_volume,
        ask_volume=ask_volume,
        imbalance_percent=imbalance_percent(bid_volume, ask_volume),
    )


@dataclass(frozen=True)
class RejectedDepth:
    """A depth message that failed validation; counted apart from other rejected messages."""


@dataclass(frozen=True)
class ServerShutdown:
    """Binance is about to shut the server down and asks clients to reconnect right away."""

    event_time: datetime | None


ExchangeEvent = (
    MarketTicker | RollingChange | KlineUpdate | OrderBookSnapshot | RejectedDepth | ServerShutdown
)


def parse_message(
    raw: str, allowed: set[str], received_at: datetime | None = None
) -> ExchangeEvent | None:
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

    # Depth payloads have no "e" field, so they are recognised by the stream name.
    stream = message.get("stream")
    if is_depth_stream(stream):
        snapshot = parse_depth(stream, data, allowed, received_at or datetime.now(UTC))
        return RejectedDepth() if snapshot is None else snapshot

    event_type = data.get("e")
    if event_type == "24hrTicker":
        return parse_ticker(data, allowed)
    if event_type in ROLLING_EVENT_WINDOWS:
        return parse_rolling_ticker(data, allowed)
    if event_type == "kline":
        return parse_kline(data, allowed)
    if event_type == "serverShutdown":
        return ServerShutdown(event_time=_to_datetime(data.get("E")))
    return None


def build_stream_url(
    base_url: str,
    symbols: list[str],
    intervals: list[str],
    rolling_windows: list[str] | None = None,
    depth_levels: int | None = None,
) -> str:
    """depth_levels None leaves the order book stream out."""
    streams: list[str] = []
    for symbol in symbols:
        name = symbol.lower()
        streams.append(f"{name}@ticker")
        streams.extend(f"{name}@ticker_{window}" for window in rolling_windows or [])
        streams.extend(f"{name}@kline_{interval}" for interval in intervals)
        if depth_levels is not None:
            # Without the @100ms suffix Binance updates the partial book every 1000 ms.
            streams.append(f"{name}@depth{depth_levels}")
    return f"{base_url.rstrip('/')}/stream?streams={'/'.join(streams)}"


def compute_backoff(attempt: int, initial: float, maximum: float) -> float:
    return min(initial * 2**attempt, maximum)


class ServerShutdownError(Exception):
    """Raised inside the receive loop to leave the connection after a serverShutdown event."""


class ExchangeUnavailableError(Exception):
    """Binance REST could not give a usable answer (network error, 5xx or rate limit)."""


def parse_rest_kline(row: Any, symbol: str, interval: str, is_closed: bool) -> KlineUpdate | None:
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
    """Candles and fallback tickers from Binance REST (public market data endpoint only)."""

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
        rows = await self._get_list(
            "/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit}
        )
        last_index = len(rows) - 1
        # The newest candle is still being built, every earlier one is final.
        candles = [
            parse_rest_kline(row, symbol, interval, is_closed=index != last_index)
            for index, row in enumerate(rows)
        ]
        return [candle for candle in candles if candle is not None]

    async def get_kline_at(self, symbol: str, interval: str, start: datetime) -> KlineUpdate | None:
        """The first candle opening at or after `start`, or None if Binance has none."""
        rows = await self._get_list(
            "/api/v3/klines",
            {
                "symbol": symbol,
                "interval": interval,
                "startTime": datetime_to_ms(start),
                "limit": 1,
            },
        )
        if not rows:
            return None
        candle = parse_rest_kline(rows[0], symbol, interval, is_closed=False)
        if candle is None:
            return None
        return candle.model_copy(update={"is_closed": candle.close_time < datetime.now(UTC)})

    async def get_tickers(self, symbols: list[str]) -> list[MarketTicker]:
        """24h tickers for all `symbols` in one request (weight 2 for up to 20 symbols)."""
        # Binance expects a JSON array without spaces: symbols=["BTCEUR","ETHEUR"].
        entries = await self._get_list(
            "/api/v3/ticker/24hr", {"symbols": json.dumps(symbols, separators=(",", ":"))}
        )
        tickers = [parse_rest_ticker(entry, set(symbols)) for entry in entries]
        return [ticker for ticker in tickers if ticker is not None]

    async def _get_list(self, path: str, params: dict[str, Any]) -> list[Any]:
        if time.monotonic() < self._paused_until:
            # After 429/418 Binance bans clients that keep retrying, so wait out the pause.
            raise ExchangeUnavailableError("Binance rate limit pause is still active")

        try:
            response = await self.http.get(path, params=params)
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
        return rows

    def _retry_after(self, response: httpx.Response) -> float:
        try:
            return max(float(response.headers["Retry-After"]), 1.0)
        except (KeyError, ValueError):
            return self.DEFAULT_RATE_LIMIT_PAUSE


ClientStatus = Literal["connecting", "connected", "reconnecting", "stopped"]
TickerCallback = Callable[[MarketTicker], Awaitable[None]]
KlineCallback = Callable[[KlineUpdate], Awaitable[None]]
RollingCallback = Callable[[RollingChange], Awaitable[None]]
DepthCallback = Callable[[OrderBookSnapshot], Awaitable[None]]
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

    # Binance allows 300 new connections per 5 minutes per IP; a second serverShutdown within
    # this many seconds falls back to the normal backoff so a shutdown loop cannot burn it.
    SERVER_SHUTDOWN_COOLDOWN_SECONDS = 60.0

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
        on_rolling: RollingCallback | None = None,
        on_depth: DepthCallback | None = None,
        stats: RuntimeStats | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url = url
        self.on_ticker = on_ticker
        self.on_kline = on_kline
        self.on_rolling = on_rolling
        self.on_depth = on_depth
        self.on_status_change = on_status_change
        self.connect_factory = connect_factory
        self.allowed_symbols = allowed_symbols
        self.receive_timeout = receive_timeout
        self.reconnect_initial_delay = reconnect_initial_delay
        self.reconnect_max_delay = reconnect_max_delay
        self.stats = stats or RuntimeStats()
        self.clock = clock

        self.status: ClientStatus = "stopped"
        self.last_message_at: datetime | None = None
        self._stop_event = asyncio.Event()
        self._connection: Any = None
        self._last_server_shutdown_at: float | None = None

    async def run(self) -> None:
        self._stop_event.clear()
        await self._set_status("connecting")
        attempt = 0
        while not self._stop_event.is_set():
            reason = "connection closed"
            server_shutdown = False
            try:
                async with self.connect_factory(self.url) as connection:
                    self._connection = connection
                    await self._set_status("connected")
                    logger.info("Connected to Binance stream after %d failed attempt(s)", attempt)
                    attempt = 0
                    await self._receive_loop(connection)
            except asyncio.CancelledError:
                raise
            except ServerShutdownError:
                # Leaving the `async with` has already closed the connection.
                server_shutdown = True
                reason = "server shutdown"
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                reason = f"{type(exc).__name__}: {exc}"
            finally:
                self._connection = None

            if self._stop_event.is_set():
                break
            if server_shutdown:
                self.stats.record_server_shutdown()
                if self._shutdown_allows_immediate_reconnect():
                    self.stats.record_disconnect(reason)
                    await self._set_status("reconnecting")
                    logger.info("Binance server is shutting down; reconnecting immediately")
                    continue
                reason = "repeated server shutdown"
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

    def _shutdown_allows_immediate_reconnect(self) -> bool:
        now = self.clock()
        previous = self._last_server_shutdown_at
        self._last_server_shutdown_at = now
        return previous is None or now - previous > self.SERVER_SHUTDOWN_COOLDOWN_SECONDS

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
        if isinstance(event, ServerShutdown):
            raise ServerShutdownError
        if isinstance(event, RejectedDepth):
            self.stats.record_depth_message(accepted=False)
            self.stats.record_rejected_message()
            return
        if isinstance(event, OrderBookSnapshot):
            self.stats.record_depth_message(accepted=True)
        else:
            # Depth arrives every second regardless of trading, so it would hide a silent
            # ticker or kline stream; seconds_since_last_message keeps tracking only those.
            self.stats.record_pair_message(event.symbol)
        try:
            if isinstance(event, OrderBookSnapshot):
                if self.on_depth is not None:
                    await self.on_depth(event)
            elif isinstance(event, MarketTicker):
                await self.on_ticker(event)
            elif isinstance(event, KlineUpdate):
                await self.on_kline(event)
            elif isinstance(event, RollingChange) and self.on_rolling is not None:
                await self.on_rolling(event)
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
