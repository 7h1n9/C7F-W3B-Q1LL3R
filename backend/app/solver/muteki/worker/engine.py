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


def get_engine(engine_type: str, *, cli: bool = False) -> WorkerEngine:
    """Create one of the supported engines without importing optional CLIs eagerly.

    The registry lives in ``.engines`` next to the classes themselves, so adding
    an engine does not need a second parallel name map here.

    ``cli`` is accepted for call-site compatibility and does not change the
    result: every engine currently registered is a CLI engine.
    """

    del cli
    from .engines import ENGINE_CLASSES

    factory = ENGINE_CLASSES.get(str(engine_type or "").strip().casefold())
    if factory is None:
        raise ValueError(f"unsupported Muteki engine: {engine_type}")
    return factory()


__all__ = ["WorkerEngine", "WorkerResult", "get_engine", "intent_prompt"]
