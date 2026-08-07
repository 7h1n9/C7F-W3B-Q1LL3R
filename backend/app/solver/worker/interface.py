from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..action import ActionIntent


@dataclass
class WorkerResult:
    """Execution result owned by Solver Core, independent of legacy task results."""

    success: bool = False
    action_name: str = ""
    output: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    facts: list[dict[str, Any]] = field(default_factory=list)
    hypotheses: list[dict[str, Any]] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)

    def __init__(
        self,
        success: bool | None = None,
        action_name: str = "",
        output: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        *,
        status: str | None = None,
        observation: dict[str, Any] | None = None,
        facts: list[dict[str, Any]] | None = None,
        hypotheses: list[dict[str, Any]] | None = None,
        evidence_refs: list[str] | None = None,
    ) -> None:
        """Build the new contract while reading the Phase 1.1 compatibility shape."""
        normalized_status = str(status or "").upper()
        if success is None:
            success = normalized_status in {"SUCCESS", "COMPLETED", "OK"}

        self.success = bool(success)
        self.action_name = action_name
        self.output = dict(output if output is not None else observation or {})
        self.metadata = dict(metadata or {})
        if status is not None:
            self.metadata.setdefault("status", status)
        self.facts = list(facts or [])
        self.hypotheses = list(hypotheses or [])
        self.evidence_refs = list(evidence_refs or [])

    @property
    def status(self) -> str:
        """Compatibility view; new callers should use ``success`` and metadata."""
        value = self.metadata.get("status")
        if value is not None:
            return str(value)
        return "SUCCESS" if self.success else "FAILED"

    @property
    def observation(self) -> dict[str, Any]:
        """Compatibility view; the canonical field is ``output``."""
        return self.output


class Worker(ABC):
    """Execution boundary consumed by WorkerManager and hidden from SolverLoop."""

    @abstractmethod
    async def execute(self, action: ActionIntent) -> WorkerResult:
        raise NotImplementedError
