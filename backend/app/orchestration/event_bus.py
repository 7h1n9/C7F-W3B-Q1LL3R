import asyncio
from collections import defaultdict


class InMemoryEventBus:
    """Process-local fanout; persistence remains the source of truth."""
    def __init__(self) -> None:
        self._queues: dict[str, set[asyncio.Queue]] = defaultdict(set)

    async def publish(self, run_id: str, event: dict) -> None:
        for queue in list(self._queues[run_id]):
            await queue.put(event)

    async def subscribe(self, run_id: str):
        queue: asyncio.Queue = asyncio.Queue()
        self._queues[run_id].add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._queues[run_id].discard(queue)


event_bus = InMemoryEventBus()
