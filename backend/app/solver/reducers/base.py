from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..observation import SolverObservation


@dataclass(frozen=True)
class KnowledgeUpdate:
    """Reducer output containing only durable cognition, never raw output."""

    verified_facts: list[dict] = field(default_factory=list)
    hypotheses: list[dict] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    next_phase: str | None = None
    control_updates: dict = field(default_factory=dict)


class ObservationReducer(Protocol):
    def reduce(self, observation: SolverObservation) -> KnowledgeUpdate: ...
