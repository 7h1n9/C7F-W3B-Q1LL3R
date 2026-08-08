from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .events import EventType
from .graph import Intent, MutekiGraph


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
    payload: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkerOutcome:
    worker_id: str
    status: str
    flag_found: bool = False
    result: str = ""


WorkerRunner = Callable[[WorkerJob], Awaitable[WorkerOutcome]]
ReviewHandler = Callable[[WorkerJob], Awaitable[WorkerOutcome]]


class MutekiWorkerPool:
    """One-shot worker tasks with explicit capacity and cancellation."""

    def __init__(self, graph: MutekiGraph, runner: WorkerRunner, *, max_workers: int = 10, review_handler: ReviewHandler | None = None, engine_pool: Any | None = None, external_runner: WorkerRunner | None = None) -> None:
        self.graph = graph
        self.runner = runner
        self.max_workers = max(1, max_workers)
        self.review_handler = review_handler
        self.engine_pool = engine_pool
        self.external_runner = external_runner
        self._tasks: dict[str, asyncio.Task[WorkerOutcome]] = {}
        self._jobs: dict[str, WorkerJob] = {}

    @property
    def active_count(self) -> int:
        return sum(not task.done() for task in self._tasks.values())

    @property
    def active_engine_ids(self) -> frozenset[str]:
        return frozenset(job.engine_id for worker_id, job in self._jobs.items() if not self._tasks[worker_id].done())

    def get_available_engine(self, preferred: str | None = None) -> str | None:
        candidates = ([preferred] if preferred else []) + [job.engine_id for job in self._jobs.values() if job.engine_id not in {preferred}]
        for candidate in candidates:
            if candidate and candidate not in self.active_engine_ids:
                return candidate
        return None

    async def spawn(self, job: WorkerJob) -> bool:
        if self.active_count >= self.max_workers or job.worker_id in self._tasks:
            return False
        self.graph.emit_event(actor=job.worker_id, event_type=EventType.WORKER_STARTED, payload={"role": job.role, "engine_id": job.engine_id, "intent_id": job.intent_id})
        task = asyncio.create_task(self._run(job), name=f"muteki-worker-{job.worker_id}")
        self._tasks[job.worker_id] = task
        self._jobs[job.worker_id] = job
        return True

    async def _run(self, job: WorkerJob) -> WorkerOutcome:
        try:
            if job.role == "review" and self.review_handler is not None:
                return await self.review_handler(job)
            if self.external_runner is not None and job.intent_id:
                return await self.external_runner(job)
            if self.engine_pool is not None:
                intent = Intent(
                    job.intent_id or f"worker-{job.worker_id}",
                    job.goal or job.role,
                    "open",
                    None,
                    "",
                    payload=dict(job.payload),
                )
                workspace = str(job.environment.get("MUTEKI_WORKSPACE") or Path(self.graph.db_path).parent.parent)
                result = await self.engine_pool.execute(intent, workspace, preferred=job.engine_id)
                return WorkerOutcome(job.worker_id, "COMPLETED" if result.success else "FAILED", result=result.output or str(result.metadata.get("reason") or ""))
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
