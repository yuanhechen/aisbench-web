import asyncio
import logging
from collections import defaultdict
from threading import Lock

logger = logging.getLogger(__name__)

# Bounded so a client that stops reading cannot grow memory without limit; REST remains
# authoritative, so dropping the oldest event costs nothing a reconnect cannot recover.
SUBSCRIBER_QUEUE_SIZE = 64


class JobNotifier:
    """Process-local fan-out of job events, one asyncio queue per connected subscriber."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._guard = Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Record the serving loop so the worker thread can publish into it."""
        self._loop = loop

    def subscribe(self, job_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        with self._guard:
            self._subscribers[job_id].append(queue)
        return queue

    def unsubscribe(self, job_id: str, queue: asyncio.Queue) -> None:
        with self._guard:
            queues = self._subscribers.get(job_id)
            if queues is None:
                return
            if queue in queues:
                queues.remove(queue)
            if not queues:
                self._subscribers.pop(job_id, None)

    def subscriber_count(self, job_id: str) -> int:
        with self._guard:
            return len(self._subscribers.get(job_id, ()))

    def publish(self, job_id: str, event: dict) -> None:
        """Deliver to current subscribers, dropping the oldest event on a full queue."""
        with self._guard:
            queues = list(self._subscribers.get(job_id, ()))
        for queue in queues:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    logger.debug("Dropped a job event for %s", job_id)

    def publish_threadsafe(self, job_id: str, event: dict) -> None:
        """Publish from the worker thread into the loop that owns the subscriber queues."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self.publish, job_id, event)
        except RuntimeError:
            logger.debug("Event loop is gone; dropping a job event for %s", job_id)
