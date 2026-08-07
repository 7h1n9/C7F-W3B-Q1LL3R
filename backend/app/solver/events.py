from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class SolverEvent:
    """An append-only event describing one Solver state change."""

    event_type: str
    action: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.event_type,
            "action": self.action,
            "payload": dict(self.payload),
            "timestamp": self.timestamp.isoformat(),
        }
