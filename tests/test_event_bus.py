"""EventBus: one bounded queue and one worker per subscriber, with "latest" and "keep_all"."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.config import Settings
from app.schemas.market import KlineUpdate, MarketTicker
from app.services.event_bus import EventBus
from app.services.market_data_service import MarketDataService, latest_event_key


def make_ticker(price: str, seconds: int = 0, symbol: str = "BTCEUR") -> MarketTicker:
    return MarketTicker(
        symbol=symbol,
        display_symbol=f"{symbol[:-3]}/EUR",
        last_price=Decimal(price),
        event_time=datetime(2026, 10, 30, tzinfo=UTC) + timedelta(seconds=seconds),
    )


def make_kline(open_time: datetime, closed: bool) -> KlineUpdate:
    return KlineUpdate(
        symbol="BTCEUR",
        interval="1m",
        open_time=open_time,
        close_time=open_time + timedelta(seconds=59),
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=Decimal("1"),
        is_closed=closed,
    )


def ticker_event(number: int) -> dict:
    return {"type": "ticker", "data": make_ticker(str(70000 + number), seconds=number)}


def make_bus(
    latest_size: int = 10,
    keep_all_size: int = 10,
    put_timeout: float = 1.0,
    drain_timeout: float = 5.0,
) -> EventBus:
    return EventBus(
        latest_size,
        keep_all_size,
        latest_event_key,
        put_timeout=put_timeout,
        drain_timeout=drain_timeout,
    )


class GatedRecorder:
    """A subscriber that holds on to each event until the test opens the gate."""

    def __init__(self) -> None:
        self.received: list[dict] = []
        self.gate = asyncio.Event()

    async def __call__(self, event: dict) -> None:
        await self.gate.wait()
        self.received.append(event)


def prices(events: list[dict]) -> list[Decimal]:
    return [event["data"].last_price for event in events]


# ---------- "latest" ----------


async def test_latest_keeps_only_the_newest_price_of_a_pair():
    service = MarketDataService(Settings(_env_file=None))  # never started
    browser = GatedRecorder()
    service.subscribe(browser, name="browser_broadcast", policy="latest")
    subscription = service.events.subscriptions[0]

    for second in range(100):
        await service.handle_ticker(make_ticker(str(72000 + second), seconds=second))
        # The first price is already with the busy worker; the rest share one slot.
        assert subscription.queue.qsize() <= 1

    browser.gate.set()
    await service.events.join()
    await service.stop()

    assert prices(browser.received) == [Decimal("72000"), Decimal("72099")]
    assert subscription.dropped == 0


async def test_latest_keeps_one_waiting_event_per_pair():
    bus = make_bus()
    browser = GatedRecorder()
    bus.subscribe(browser, "browser", "latest")

    await bus.publish({"type": "status", "data": {}})  # taken by the worker, which waits
    for second, symbol in enumerate(["BTCEUR", "ETHEUR", "BTCEUR", "ETHEUR"]):
        await bus.publish(
            {"type": "ticker", "data": make_ticker(str(100 + second), second, symbol)}
        )

    browser.gate.set()
    await bus.stop()

    tickers = [event["data"] for event in browser.received if event["type"] == "ticker"]
    assert [(t.symbol, t.last_price) for t in tickers] == [
        ("BTCEUR", Decimal("102")),
        ("ETHEUR", Decimal("103")),
    ]


async def test_latest_does_not_replace_a_closed_candle_with_the_next_one():
    open_time = datetime(2026, 10, 30, tzinfo=UTC)
    events = [
        {"type": "kline", "data": make_kline(open_time, closed=True)},
        {"type": "kline", "data": make_kline(open_time + timedelta(minutes=1), closed=False)},
    ]

    assert latest_event_key(events[0]) != latest_event_key(events[1])


# ---------- "keep_all" ----------


async def test_keep_all_loses_nothing_while_the_queue_has_room():
    bus = make_bus(keep_all_size=5)
    storage = GatedRecorder()
    subscription = bus.subscribe(storage, "storage", "keep_all")

    # One event with the worker plus five waiting: exactly what fits.
    for number in range(6):
        await bus.publish(ticker_event(number))
    assert subscription.queue.qsize() == 5

    storage.gate.set()
    await bus.stop()

    assert prices(storage.received) == [Decimal(70000 + n) for n in range(6)]
    assert subscription.dropped == 0


async def test_keep_all_waits_for_room_before_giving_up():
    bus = make_bus(keep_all_size=1, put_timeout=1.0)
    storage = GatedRecorder()
    subscription = bus.subscribe(storage, "storage", "keep_all")
    await bus.publish(ticker_event(0))  # with the worker
    await bus.publish(ticker_event(1))  # fills the queue

    # The subscriber catches up while the producer waits, so the third event still fits.
    asyncio.get_running_loop().call_later(0.05, storage.gate.set)
    await bus.publish(ticker_event(2))
    await bus.stop()

    assert len(storage.received) == 3
    assert subscription.dropped == 0


async def test_full_keep_all_queue_drops_after_timeout_and_counts(caplog):
    bus = make_bus(keep_all_size=2, put_timeout=0.05)
    storage = GatedRecorder()
    subscription = bus.subscribe(storage, "candle_storage", "keep_all")

    with caplog.at_level(logging.ERROR, logger="app.services.event_bus"):
        for number in range(5):  # 1 with the worker + 2 waiting, then 2 that do not fit
            await bus.publish(ticker_event(number))

    assert subscription.dropped == 2
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 2
    assert "candle_storage" in errors[0].getMessage()

    storage.gate.set()
    await bus.stop()
    assert prices(storage.received) == [Decimal("70000"), Decimal("70001"), Decimal("70002")]


# ---------- Errors and shutdown ----------


async def test_failing_subscriber_keeps_its_worker_running(caplog):
    bus = make_bus()
    handled = []

    async def flaky(event):
        if event["data"].last_price == Decimal("70000"):
            raise RuntimeError("boom")
        handled.append(event)

    subscription = bus.subscribe(flaky, "flaky", "keep_all")
    with caplog.at_level(logging.ERROR, logger="app.services.event_bus"):
        await bus.publish(ticker_event(0))
        await bus.publish(ticker_event(1))
        await bus.join()

    assert prices(handled) == [Decimal("70001")]
    assert subscription.worker is not None and not subscription.worker.done()
    assert "flaky failed" in caplog.text
    await bus.stop()


async def test_stop_drains_queues_and_leaves_no_tasks():
    bus = make_bus()
    handled = []

    async def slow(event):
        await asyncio.sleep(0.01)
        handled.append(event)

    bus.subscribe(slow, "slow", "keep_all")
    for number in range(5):
        await bus.publish(ticker_event(number))

    await bus.stop()

    assert len(handled) == 5
    assert asyncio.all_tasks() == {asyncio.current_task()}

    # Once stopped, new events are refused instead of starting new workers.
    await bus.publish(ticker_event(5))
    assert len(handled) == 5
    assert asyncio.all_tasks() == {asyncio.current_task()}


async def test_stop_cancels_a_stuck_subscriber_after_the_timeout(caplog):
    bus = make_bus(drain_timeout=0.05)
    stuck = GatedRecorder()  # the gate never opens
    bus.subscribe(stuck, "stuck", "keep_all")
    await bus.publish(ticker_event(0))
    await bus.publish(ticker_event(1))

    with caplog.at_level(logging.WARNING, logger="app.services.event_bus"):
        await bus.stop()

    assert stuck.received == []
    assert asyncio.all_tasks() == {asyncio.current_task()}
    assert "1 unhandled event(s) for stuck" in caplog.text
