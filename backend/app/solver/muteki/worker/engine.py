from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..graph import Intent


@dataclass(frozen=True, slots=True)
class WorkerResult:
    """Sanitized result returned by one heterogeneous engine invocation."""

    success: bool
    engine_type: str
    output: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class WorkerEngine(ABC):
    """One execution backend behind the canonical Worker boundary."""

    @abstractmethod
    async def execute(self, intent: Intent, workspace: str) -> WorkerResult:
        """Execute one intent in the supplied isolated workspace."""

    @abstractmethod
    def engine_type(self) -> str:
        """Return the stable engine name used by scheduling and audit events."""

    @abstractmethod
    def health_check(self) -> bool:
        """Return whether this engine can be selected without starting a job."""


def intent_prompt(intent: Intent) -> str:
    """Build a bounded prompt without serializing arbitrary graph state."""

    payload = intent.payload or {}
    details = payload.get("prompt") or payload.get("instruction") or ""
    return f"Intent: {intent.description}\n{details}".strip()


__all__ = ["WorkerEngine", "WorkerResult", "intent_prompt"]
