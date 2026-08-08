from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from .events import EventType
from .graph import MutekiGraph


@dataclass(frozen=True, slots=True)
class EngineProfile:
    engine_id: str
    command: tuple[str, ...] = ()
    healthy: bool = True
    worker_class: str = "code"
    environment: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkerJob:
    worker_id: str
    role: str
    engine_id: str
    graph_path: str
    challenge_id: str
    intent_id: str | None = None
    goal: str = ""
    environment: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkerOutcome:
    worker_id: str
    status: str
    flag_found: bool = False
    result: str = ""


WorkerRunner = Callable[[WorkerJob], Awaitable[WorkerOutcome]]


class MutekiWorkerPool:
    """One-shot worker tasks with explicit capacity and cancellation."""

    def __init__(self, graph: MutekiGraph, runner: WorkerRunner, *, max_workers: int = 10) -> None:
        self.graph = graph
        self.runner = runner
        self.max_workers = max(1, max_workers)
        self._tasks: dict[str, asyncio.Task[WorkerOutcome]] = {}

    @property
    def active_count(self) -> int:
        return sum(not task.done() for task in self._tasks.values())

    async def spawn(self, job: WorkerJob) -> bool:
        if self.active_count >= self.max_workers or job.worker_id in self._tasks:
            return False
        self.graph.emit_event(actor=job.worker_id, event_type=EventType.WORKER_STARTED, payload={"role": job.role, "engine_id": job.engine_id, "intent_id": job.intent_id})
        task = asyncio.create_task(self._run(job), name=f"muteki-worker-{job.worker_id}")
        self._tasks[job.worker_id] = task
        return True

    async def _run(self, job: WorkerJob) -> WorkerOutcome:
        try:
            return await self.runner(job)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return WorkerOutcome(job.worker_id, "FAILED", result=str(error))
        finally:
            self.graph.emit_event(actor=job.worker_id, event_type=EventType.WORKER_FINISHED, payload={"role": job.role, "engine_id": job.engine_id, "intent_id": job.intent_id})

    async def wait(self) -> tuple[WorkerOutcome | BaseException, ...]:
        tasks = tuple(self._tasks.values())
        if not tasks:
            return ()
        return tuple(await asyncio.gather(*tasks, return_exceptions=True))

    async def cancel_all(self) -> None:
        tasks = tuple(task for task in self._tasks.values() if not task.done())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


__all__ = ["EngineProfile", "MutekiWorkerPool", "WorkerJob", "WorkerOutcome"]
