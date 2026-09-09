from __future__ import annotations

from collections.abc import Mapping

from ..action import ActionIntent
from .adapters.runner import RunnerWorker
from .interface import Worker, WorkerResult


class WorkerUnavailable(RuntimeError):
    """Raised when an ActionIntent names no registered execution backend."""


class WorkerManager:
    """Route an ActionIntent to an explicit Worker backend without fallback."""

    def __init__(self, workers: Mapping[str, Worker] | None = None) -> None:
        self._workers: dict[str, Worker] = {
            "runner": RunnerWorker(),
        }
        if workers:
            self._workers.update(workers)

    def register(self, backend: str, worker: Worker) -> None:
        self._workers[backend] = worker

    async def execute(self, action: ActionIntent) -> WorkerResult:
        backend = action.metadata.get("backend", "runner")
        worker = self._workers.get(backend)
        if worker is None:
            raise WorkerUnavailable(
                f"No Worker registered for backend {backend!r} "
                f"(action {action.action_name!r})"
            )
        return await worker.execute(action)

