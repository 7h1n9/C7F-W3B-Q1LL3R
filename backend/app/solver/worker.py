from __future__ import annotations

from typing import Any, Protocol

from .action import ActionIntent


class WorkerResult:
    """Minimal action result; raw tool output is intentionally out of scope."""

    def __init__(
        self,
        *,
        status: str,
        observation: dict[str, Any] | None = None,
        facts: list[dict[str, Any]] | None = None,
        hypotheses: list[dict[str, Any]] | None = None,
        evidence_refs: list[str] | None = None,
    ) -> None:
        self.status = status
        self.observation = dict(observation or {})
        self.facts = list(facts or [])
        self.hypotheses = list(hypotheses or [])
        self.evidence_refs = list(evidence_refs or [])


class Worker(Protocol):
    async def execute(self, intent: ActionIntent) -> WorkerResult: ...


class NoopWorker:
    """Placeholder worker; real execution adapters are a later phase."""

    async def execute(self, intent: ActionIntent) -> WorkerResult:
        return WorkerResult(status="NOT_IMPLEMENTED")


class MockWorker:
    """Deterministic Worker used by Solver Loop tests only."""

    def __init__(self, *, response: str = "xxx") -> None:
        self.response = response
        self.calls: list[ActionIntent] = []

    async def execute(self, intent: ActionIntent) -> WorkerResult:
        self.calls.append(intent)
        if intent.action_name == "sql_boolean_compare":
            observation = {
                "true": True,
                "false": False,
                "action": intent.action_name,
            }
        else:
            observation = {
                "response": self.response,
                "action": intent.action_name,
            }
        return WorkerResult(
            status="SUCCESS",
            observation=observation,
        )
