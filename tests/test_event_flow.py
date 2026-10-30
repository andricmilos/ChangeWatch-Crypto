"""How market events flow from MarketDataService to its subscribers, and how long that takes."""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.config import Settings
from app.schemas.market import MarketTicker
from app.services.market_data_service import MarketDataService

SLOW_SUBSCRIBER_SECONDS = 0.5
BROWSER_DEADLINE_SECONDS = 0.05


def make_ticker(price: str, seconds: int) -> MarketTicker:
    return MarketTicker(
        symbol="BTCEUR",
        display_symbol="BTC/EUR",
        last_price=Decimal(price),
        event_time=datetime(2026, 10, 30, tzinfo=UTC) + timedelta(seconds=seconds),
    )


async def test_slow_subscriber_does_not_delay_browser():
    service = MarketDataService(Settings(_env_file=None))  # never started
    received_at: list[float] = []

    async def browser(event):
        received_at.append(time.perf_counter())

    async def slow_storage(event):
        await asyncio.sleep(SLOW_SUBSCRIBER_SECONDS)

    # Same order as app/main.py: browser first, then the database writer.
    service.subscribe(browser, name="browser_broadcast")
    service.subscribe(slow_storage, name="candle_storage")

    # Two prices arrive together; the stream client hands them over one by one, like its
    # receive loop does. The second one only gets in after the first has fully been handled.
    arrived_at = time.perf_counter()
    for ticker in (make_ticker("72000", 0), make_ticker("72001", 1)):
        await service.handle_ticker(ticker)

    delays = [moment - arrived_at for moment in received_at]
    assert len(delays) == 2
    assert all(delay < BROWSER_DEADLINE_SECONDS for delay in delays), delays


def test_subscriber_name_defaults_to_qualified_name():
    service = MarketDataService(Settings(_env_file=None))

    async def record(event):
        pass

    service.subscribe(record)
    assert service.events.subscriptions[0].name.endswith("record")
