import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Hashable
from typing import Any, Literal

logger = logging.getLogger(__name__)

MarketEvent = dict[str, Any]
Subscriber = Callable[[MarketEvent], Awaitable[None]]
EventKey = Callable[[MarketEvent], Hashable]
# "latest": only the newest event per key matters (browsers). "keep_all": every event
# must arrive (alerts, candle storage).
QueuePolicy = Literal["latest", "keep_all"]

KEEP_ALL_PUT_TIMEOUT_SECONDS = 1.0
STOP_DRAIN_TIMEOUT_SECONDS = 5.0


class LatestQueue:
    """A bounded queue that keeps only the newest waiting event per key.

    A new event whose key is already waiting replaces it in place, so a busy pair cannot
    pile up stale prices and keeps its turn in the line.
    """

    def __init__(self, maxsize: int, key: EventKey) -> None:
        self.maxsize = maxsize
        self._key = key
        self._items: dict[Hashable, MarketEvent] = {}
        self._has_items = asyncio.Event()
        # Waiting events plus the one being handled; join() returns when it reaches 0.
        self._unfinished = 0
        self._all_done = asyncio.Event()
        self._all_done.set()

    def qsize(self) -> int:
        return len(self._items)

    def put(self, event: MarketEvent) -> MarketEvent | None:
        """Never waits. Returns the event pushed out to make room, if the queue was full."""
        key = self._key(event)
        if key in self._items:
            self._items[key] = event
            return None
        dropped = None
        if len(self._items) >= self.maxsize:
            # Only happens with more distinct keys than slots; the oldest is the most outdated.
            oldest_key = next(iter(self._items))
            dropped = self._items.pop(oldest_key)
        else:
            self._unfinished += 1
            self._all_done.clear()
        self._items[key] = event
        self._has_items.set()
        return dropped

    async def get(self) -> MarketEvent:
        while not self._items:
            self._has_items.clear()
            await self._has_items.wait()
        key = next(iter(self._items))
        return self._items.pop(key)

    def task_done(self) -> None:
        self._unfinished -= 1
        if self._unfinished == 0:
            self._all_done.set()

    async def join(self) -> None:
        await self._all_done.wait()


class Subscription:
    """One subscriber with its own queue and worker, plus counters for the timing report."""

    def __init__(
        self, name: str, callback: Subscriber, policy: QueuePolicy, maxsize: int, key: EventKey
    ) -> None:
        self.name = name
        self.callback = callback
        self.policy = policy
        self.queue: LatestQueue | asyncio.Queue[MarketEvent] = (
            LatestQueue(maxsize, key) if policy == "latest" else asyncio.Queue(maxsize)
        )
        self.worker: asyncio.Task[None] | None = None
        # Since start; never reset, unlike the timing window below.
        self.dropped = 0
        self.calls = 0
        self.total_seconds = 0.0
        self.max_seconds = 0.0

    def record(self, seconds: float) -> None:
        self.calls += 1
        self.total_seconds += seconds
        self.max_seconds = max(self.max_seconds, seconds)

    def reset_timings(self) -> None:
        self.calls = 0
        self.total_seconds = 0.0
        self.max_seconds = 0.0


class EventBus:
    """Hands every event to each subscriber through its own bounded queue and worker task.

    `publish` only enqueues, so a slow subscriber (e.g. the database writer) can no longer
    hold up the others or the Binance receive loop.
    """

    def __init__(
        self,
        latest_queue_size: int,
        keep_all_queue_size: int,
        key: EventKey,
        put_timeout: float = KEEP_ALL_PUT_TIMEOUT_SECONDS,
        drain_timeout: float = STOP_DRAIN_TIMEOUT_SECONDS,
    ) -> None:
        self.latest_queue_size = latest_queue_size
        self.keep_all_queue_size = keep_all_queue_size
        self.key = key
        self.put_timeout = put_timeout
        self.drain_timeout = drain_timeout
        self.subscriptions: list[Subscription] = []
        self._started = False
        self._closed = False

    def subscribe(self, callback: Subscriber, name: str, policy: QueuePolicy) -> Subscription:
        maxsize = self.latest_queue_size if policy == "latest" else self.keep_all_queue_size
        subscription = Subscription(name, callback, policy, maxsize, self.key)
        self.subscriptions.append(subscription)
        if self._started:
            self._start_worker(subscription)
        return subscription

    def start(self) -> None:
        """Start one worker per subscriber. Must run inside the event loop."""
        if self._started:
            return
        self._started = True
        for subscription in self.subscriptions:
            self._start_worker(subscription)

    def _start_worker(self, subscription: Subscription) -> None:
        subscription.worker = asyncio.create_task(
            self._work(subscription), name=f"event-bus:{subscription.name}"
        )

    async def publish(self, event: MarketEvent) -> None:
        if self._closed:
            logger.debug("Event bus is stopped; %s event ignored", event["type"])
            return
        # A service used without start() (e.g. in tests) still delivers its events.
        self.start()
        for subscription in self.subscriptions:
            if isinstance(subscription.queue, LatestQueue):
                if subscription.queue.put(event) is not None:
                    subscription.dropped += 1
                    logger.warning(
                        "Queue of %s is full; its oldest waiting event was dropped",
                        subscription.name,
                    )
            else:
                await self._put_keep_all(subscription, subscription.queue, event)
        # Socket messages that are already buffered come in without a pause, so without this
        # a burst would reach the workers only after it ended.
        await asyncio.sleep(0)

    async def _put_keep_all(
        self, subscription: Subscription, queue: asyncio.Queue[MarketEvent], event: MarketEvent
    ) -> None:
        try:
            queue.put_nowait(event)
            return
        except asyncio.QueueFull:
            pass
        # Backpressure: the producer waits for this subscriber to catch up, but only briefly,
        # because meanwhile no other subscriber gets new events either.
        try:
            await asyncio.wait_for(queue.put(event), self.put_timeout)
        except TimeoutError:
            subscription.dropped += 1
            logger.error(
                "Queue of %s still full after %.1f s; %s event dropped (%d dropped so far)",
                subscription.name,
                self.put_timeout,
                event["type"],
                subscription.dropped,
            )

    async def _work(self, subscription: Subscription) -> None:
        queue = subscription.queue
        while True:
            event = await queue.get()
            started = time.perf_counter()
            try:
                await subscription.callback(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                # One broken event must not stop this worker, nor reach the other subscribers.
                logger.exception(
                    "Market data subscriber %s failed for %s event",
                    subscription.name,
                    event["type"],
                )
            finally:
                # Failed calls count too: a subscriber that hangs before failing is still slow.
                subscription.record(time.perf_counter() - started)
                queue.task_done()

    async def join(self) -> None:
        """Wait until every event published so far has been handled."""
        await asyncio.gather(*(subscription.queue.join() for subscription in self.subscriptions))

    async def stop(self) -> None:
        """Refuse new events, give the queues `drain_timeout` s to empty, then cancel workers."""
        self._closed = True
        if not self._started:
            return
        try:
            async with asyncio.timeout(self.drain_timeout):
                await self.join()
        except TimeoutError:
            for subscription in self.subscriptions:
                if subscription.queue.qsize():
                    logger.warning(
                        "Stopping with %d unhandled event(s) for %s",
                        subscription.queue.qsize(),
                        subscription.name,
                    )
        workers = [s.worker for s in self.subscriptions if s.worker is not None]
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        for subscription in self.subscriptions:
            subscription.worker = None
