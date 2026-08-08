from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..shared_graph.bus import SolverEventBus
from .roles import WorkerRole


@dataclass(frozen=True, slots=True)
class WorkerJob:
    worker_id: str
    role: WorkerRole
    intent_id: str | None = None
    description: str = ""


WorkerRunner = Callable[[WorkerJob], Awaitable[Any]]


class OneShotWorkerPool:
    """Bounded asynchronous pool with one-shot worker semantics.

    This is an orchestration boundary, not a replacement for the existing
    Solver Worker interface.  A process/container launcher can be injected as
    ``runner`` later; tests can use an async function without spawning OS
    processes.
    """

    def __init__(
        self,
        runner: WorkerRunner,
        *,
        max_workers: int = 10,
        event_bus: SolverEventBus | None = None,
        run_id: str = "",
    ) -> None:
        self.runner = runner
        self.max_workers = max(1, max_workers)
        self.event_bus = event_bus
        self.run_id = run_id
        self._tasks: dict[str, asyncio.Task[Any]] = {}

    @property
    def active_count(self) -> int:
        return sum(not task.done() for task in self._tasks.values())

    def active_worker_ids(self) -> tuple[str, ...]:
        return tuple(worker_id for worker_id, task in self._tasks.items() if not task.done())

    async def spawn(self, job: WorkerJob) -> bool:
        if self.active_count >= self.max_workers or job.worker_id in self._tasks:
            return False
        self._emit("WORKER_STARTED", worker_id=job.worker_id, role=job.role.value, intent_id=job.intent_id)
        task = asyncio.create_task(self._run(job), name=f"solver-worker-{job.worker_id}")
        self._tasks[job.worker_id] = task
        return True

    async def _run(self, job: WorkerJob) -> Any:
        try:
            return await self.runner(job)
        finally:
            self._emit("WORKER_FINISHED", worker_id=job.worker_id, role=job.role.value, intent_id=job.intent_id)

    async def wait(self) -> None:
        tasks = tuple(self._tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def cancel_all(self) -> None:
        tasks = tuple(task for task in self._tasks.values() if not task.done())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _emit(self, event_type: str, **payload: Any) -> None:
        if self.event_bus:
            self.event_bus.emit(event_type, run_id=self.run_id, payload=payload)


__all__ = ["OneShotWorkerPool", "WorkerJob", "WorkerRole"]
