from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .action import ActionIntent
from .worker.interface import WorkerResult


@dataclass(frozen=True)
class SolverObservation:
    """Internal Worker-result protocol; raw_result is never persisted."""

    action_name: str
    success: bool
    raw_result: dict[str, Any] = field(default_factory=dict)
    facts: list[dict[str, Any]] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)

    @classmethod
    def from_worker_result(cls, intent: ActionIntent, result: WorkerResult) -> "SolverObservation":
        return cls(
            action_name=intent.action_name,
            success=result.success,
            raw_result=dict(result.output),
            facts=list(result.facts),
            evidence_refs=list(result.evidence_refs),
        )
