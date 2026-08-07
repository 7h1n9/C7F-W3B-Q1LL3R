from __future__ import annotations

from ..action import ActionIntent
from .interface import Worker, WorkerResult


class MockWorker(Worker):
    """Deterministic execution backend used by Solver Core tests."""

    def __init__(self, *, response: str = "xxx") -> None:
        self.response = response
        self.calls: list[ActionIntent] = []

    async def execute(self, action: ActionIntent) -> WorkerResult:
        self.calls.append(action)
        if action.action_name == "sql_boolean_compare":
            output = {
                "true": True,
                "false": False,
            }
        else:
            output = {
                "status_code": 200,
                "response": self.response,
                "body": self.response,
            }
        return WorkerResult(
            success=True,
            action_name=action.action_name,
            output=output,
            metadata={"backend": "mock", "status": "SUCCESS"},
        )


class NoopWorker(Worker):
    """Placeholder for callers that need an explicit non-executing Worker."""

    async def execute(self, action: ActionIntent) -> WorkerResult:
        return WorkerResult(
            success=False,
            action_name=action.action_name,
            metadata={"backend": "noop", "status": "NOT_IMPLEMENTED"},
        )
