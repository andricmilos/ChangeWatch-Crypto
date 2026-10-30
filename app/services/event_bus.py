import asyncio
import logging
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable, Hashable
from typing import Any, Literal

logger = logging.getLogger(__name__)

MarketEvent = dict[str, Any]
Subscriber = Callable[[MarketEvent], Awaitable[None]]
EventKey = Callable[[MarketEvent], Hashable]
# "latest": only the newest event per key matters (browsers). "keep_all": every event
# must arrive (alerts, candle storage).
QueuePolicy = Literal["latest", "keep_all"]
# An event together with the clock value at which it was published.
QueuedEvent = tuple[float, MarketEvent]
PutResult = Literal["added", "replaced", "dropped_oldest"]

KEEP_ALL_PUT_TIMEOUT_SECONDS = 1.0
STOP_DRAIN_TIMEOUT_SECONDS = 5.0

LATENCY_SAMPLES = 1000
FULL_WARNING_RATIO = 0.8
SLOW_WARNING_SECONDS = 0.5
WARNING_INTERVAL_SECONDS = 60.0
# p95 sorts the whole window, so it is not worth recomputing after every event.
LATENCY_CHECK_INTERVAL_SECONDS = 1.0


class LatencyWindow:
    """The last `size` delays from publish to handled, for an average and a p95."""

    def __init__(self, size: int = LATENCY_SAMPLES) -> None:
        self._samples: deque[float] = deque(maxlen=size)

    def __len__(self) -> int:
        return len(self._samples)

    def add(self, seconds: float) -> None:
        self._samples.append(seconds)

    def average(self) -> float | None:
        return sum(self._samples) / len(self._samples) if self._samples else None

    def p95(self) -> float | None:
        """Nearest rank: the smallest delay that at least 95% of the samples do not exceed."""
        if not self._samples:
            return None
        ordered = sorted(self._samples)
        return ordered[math.ceil(0.95 * len(ordered)) - 1]


def _to_ms(seconds: float | None) -> float | None:
    return None if seconds is None else round(seconds * 1000, 1)


class LatestQueue:
    """A bounded queue that keeps only the newest waiting event per key.

    A new event whose key is already waiting replaces it in place, so a busy pair cannot
    pile up stale prices and keeps its turn in the line.
    """

    def __init__(self, maxsize: int, key: EventKey) -> None:
        self.maxsize = maxsize
        self._key = key
        self._items: dict[Hashable, QueuedEvent] = {}
        self._has_items = asyncio.Event()
        # Waiting events plus the one being handled; join() returns when it reaches 0.
        self._unfinished = 0
        self._all_done = asyncio.Event()
        self._all_done.set()

    def qsize(self) -> int:
        return len(self._items)

    def put(self, item: QueuedEvent) -> PutResult:
        """Never waits. "dropped_oldest": the queue was full and its oldest event made room."""
        key = self._key(item[1])
        if key in self._items:
            # The newer event also brings its own publish time, so its delay is measured
            # from when this data arrived, not from when the slot was first taken.
            self._items[key] = item
            return "replaced"
        result: PutResult = "added"
        if len(self._items) >= self.maxsize:
            # Only happens with more distinct keys than slots; the oldest is the most outdated.
            oldest_key = next(iter(self._items))
            del self._items[oldest_key]
            result = "dropped_oldest"
        else:
            self._unfinished += 1
            self._all_done.clear()
        self._items[key] = item
        self._has_items.set()
        return result

    async def get(self) -> QueuedEvent:
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
    """One subscriber with its own queue and worker, plus the counters behind /api/status."""

    def __init__(
        self, name: str, callback: Subscriber, policy: QueuePolicy, maxsize: int, key: EventKey
    ) -> None:
        self.name = name
        self.callback = callback
        self.policy = policy
        self.queue: LatestQueue | asyncio.Queue[QueuedEvent] = (
            LatestQueue(maxsize, key) if policy == "latest" else asyncio.Queue(maxsize)
        )
        self.worker: asyncio.Task[None] | None = None
        # Counters since start, never reset.
        self.processed = 0
        self.replaced = 0
        self.dropped = 0
        self.max_size_seen = 0
        self.latencies = LatencyWindow()
        self.warned_at: float | None = None
        self.latency_checked_at = -math.inf

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "policy": self.policy,
            "size": self.queue.qsize(),
            "capacity": self.queue.maxsize,
            "max_size_seen": self.max_size_seen,
            "processed": self.processed,
            "replaced": self.replaced,
            "dropped": self.dropped,
            "latency_samples": len(self.latencies),
            "latency_avg_ms": _to_ms(self.latencies.average()),
            "latency_p95_ms": _to_ms(self.latencies.p95()),
        }


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
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.latest_queue_size = latest_queue_size
        self.keep_all_queue_size = keep_all_queue_size
        self.key = key
        self.put_timeout = put_timeout
        self.drain_timeout = drain_timeout
        # Measures queue delays and spaces out warnings; tests pass a fake one.
        self.clock = clock
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
        # One timestamp for all queues: a keep_all wait for room counts as delay too.
        item = (self.clock(), event)
        for subscription in self.subscriptions:
            if isinstance(subscription.queue, LatestQueue):
                result = subscription.queue.put(item)
                if result == "replaced":
                    subscription.replaced += 1
                elif result == "dropped_oldest":
                    subscription.dropped += 1
                    logger.warning(
                        "Queue of %s is full; its oldest waiting event was dropped",
                        subscription.name,
                    )
            else:
                await self._put_keep_all(subscription, subscription.queue, item)
            subscription.max_size_seen = max(subscription.max_size_seen, subscription.queue.qsize())
            if subscription.queue.qsize() > FULL_WARNING_RATIO * subscription.queue.maxsize:
                self._warn(subscription)
        # Socket messages that are already buffered come in without a pause, so without this
        # a burst would reach the workers only after it ended.
        await asyncio.sleep(0)

    async def _put_keep_all(
        self, subscription: Subscription, queue: asyncio.Queue[QueuedEvent], item: QueuedEvent
    ) -> None:
        try:
            queue.put_nowait(item)
            return
        except asyncio.QueueFull:
            pass
        # Backpressure: the producer waits for this subscriber to catch up, but only briefly,
        # because meanwhile no other subscriber gets new events either.
        try:
            await asyncio.wait_for(queue.put(item), self.put_timeout)
        except TimeoutError:
            subscription.dropped += 1
            logger.error(
                "Queue of %s still full after %.1f s; %s event dropped (%d dropped so far)",
                subscription.name,
                self.put_timeout,
                item[1]["type"],
                subscription.dropped,
            )

    async def _work(self, subscription: Subscription) -> None:
        queue = subscription.queue
        while True:
            published_at, event = await queue.get()
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
                subscription.processed += 1
                subscription.latencies.add(self.clock() - published_at)
                queue.task_done()
            self._check_latency(subscription)

    def _check_latency(self, subscription: Subscription) -> None:
        now = self.clock()
        if now - subscription.latency_checked_at < LATENCY_CHECK_INTERVAL_SECONDS:
            return
        subscription.latency_checked_at = now
        p95 = subscription.latencies.p95()
        if p95 is not None and p95 > SLOW_WARNING_SECONDS:
            self._warn(subscription)

    def _warn(self, subscription: Subscription) -> None:
        """One WARNING per queue per minute, with both numbers that can trigger it."""
        now = self.clock()
        if (
            subscription.warned_at is not None
            and now - subscription.warned_at < WARNING_INTERVAL_SECONDS
        ):
            return
        subscription.warned_at = now
        p95_ms = _to_ms(subscription.latencies.p95())
        logger.warning(
            "Queue of %s is falling behind: %d of %d slots used, p95 delay %s over the last "
            "%d event(s)",
            subscription.name,
            subscription.queue.qsize(),
            subscription.queue.maxsize,
            "n/a" if p95_ms is None else f"{p95_ms:.0f} ms",
            len(subscription.latencies),
        )

    def snapshot(self) -> list[dict[str, Any]]:
        return [subscription.snapshot() for subscription in self.subscriptions]

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
