from __future__ import annotations

from typing import Any, Protocol

from .planner import SolverIntent


class WorkerResult:
    """Minimal action result; raw tool output is intentionally out of scope."""

    def __init__(
        self,
        *,
        status: str,
        facts: list[dict[str, Any]] | None = None,
        hypotheses: list[dict[str, Any]] | None = None,
        evidence_refs: list[str] | None = None,
    ) -> None:
        self.status = status
        self.facts = list(facts or [])
        self.hypotheses = list(hypotheses or [])
        self.evidence_refs = list(evidence_refs or [])


class Worker(Protocol):
    async def execute(self, intent: SolverIntent) -> WorkerResult: ...


class NoopWorker:
    """Placeholder worker; real execution adapters are a later phase."""

    async def execute(self, intent: SolverIntent) -> WorkerResult:
        return WorkerResult(status="NOT_IMPLEMENTED")
