from __future__ import annotations

from ...action import ActionIntent
from ..interface import Worker, WorkerResult


class RunnerWorker(Worker):
    """Phase 1.6 placeholder; real Runner integration is a later phase."""

    async def execute(self, action: ActionIntent) -> WorkerResult:
        return WorkerResult(
            success=False,
            action_name=action.action_name,
            metadata={
                "backend": "runner",
                "status": "NOT_IMPLEMENTED",
            },
        )


RunnerAdapter = RunnerWorker
