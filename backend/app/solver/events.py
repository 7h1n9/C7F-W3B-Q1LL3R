from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class SolverAuditEventType(StrEnum):
    RUN_STARTED = "solver.run.started"
    ACTION_PLANNED = "solver.action.planned"
    ACTION_AUTHORIZED = "solver.action.authorized"
    ACTION_STARTED = "solver.action.started"
    ACTION_COMPLETED = "solver.action.completed"
    ACTION_FAILED = "solver.action.failed"
    ACTION_INTERRUPTED = "solver.action.interrupted"
    ACTION_RECOVERED = "solver.action.recovered"
    COMPLETION_EVALUATED = "solver.completion.evaluated"
    RUN_COMPLETED = "solver.run.completed"


AUDIT_EVENT_TYPES = frozenset(item.value for item in SolverAuditEventType)
_SENSITIVE_MARKERS = (
    "raw_http",
    "raw_response",
    "raw_result",
    "workerresult.output",
    "challenge_ground_truth",
    "ground_truth",
    "cookie",
    "token",
    "secret",
)


@dataclass(frozen=True, slots=True)
class SolverAuditEvent:
    """Strict, allowlisted audit metadata for Solver v2.

    This model intentionally has no payload field.  Worker output, raw
    observations, credentials, and challenge ground truth cannot be attached
    to the audit envelope accidentally.
    """

    event_type: str
    run_id: str
    step: int = 0
    phase: str = ""
    action_name: str | None = None
    action_id: str | None = None
    fingerprint: str | None = None
    status: str | None = None
    reason_code: str | None = None
    evidence_refs: tuple[str, ...] = field(default_factory=tuple)
    blackboard_version: int = 0
    source: str = "solver"
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        event_type = str(self.event_type)
        if event_type not in AUDIT_EVENT_TYPES:
            raise ValueError(f"Unsupported Solver audit event type: {event_type}")
        if not isinstance(self.run_id, str) or not self.run_id:
            raise TypeError("run_id must be a non-empty string")
        if not isinstance(self.step, int) or self.step < 0:
            raise ValueError("step must be a non-negative integer")
        if not isinstance(self.blackboard_version, int) or self.blackboard_version < 0:
            raise ValueError("blackboard_version must be a non-negative integer")
        if not isinstance(self.evidence_refs, (list, tuple)):
            raise TypeError("evidence_refs must be a list or tuple of references")
        evidence_refs = tuple(self._safe_text(item, "evidence_refs") for item in self.evidence_refs)
        object.__setattr__(self, "event_type", event_type)
        object.__setattr__(self, "evidence_refs", evidence_refs)
        for field_name in (
            "phase",
            "action_name",
            "action_id",
            "fingerprint",
            "status",
            "reason_code",
            "source",
        ):
            value = getattr(self, field_name)
            if value is not None:
                self._safe_text(value, field_name)
        if not isinstance(self.timestamp, datetime):
            raise TypeError("timestamp must be a datetime")

    @staticmethod
    def _safe_text(value: Any, field_name: str) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{field_name} must be a string")
        lowered = value.casefold()
        if any(marker in lowered for marker in _SENSITIVE_MARKERS):
            raise ValueError(f"Sensitive content is not allowed in audit field {field_name}")
        return value

    def to_dict(self) -> dict[str, Any]:
        """Serialize only the fixed audit schema; never emit arbitrary payload."""

        return {
            "event_type": self.event_type,
            "run_id": self.run_id,
            "step": self.step,
            "phase": self.phase,
            "action_name": self.action_name,
            "action_id": self.action_id,
            "fingerprint": self.fingerprint,
            "status": self.status,
            "reason_code": self.reason_code,
            "evidence_refs": list(self.evidence_refs),
            "blackboard_version": self.blackboard_version,
            "source": self.source,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass(frozen=True)
class SolverEvent:
    """An append-only event describing one Solver state change."""

    event_type: str
    action: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    audit_event: SolverAuditEvent | None = None

    def to_dict(self) -> dict[str, Any]:
        event = {
            "type": self.event_type,
            "action": self.action,
            "payload": dict(self.payload),
            "timestamp": self.timestamp.isoformat(),
        }
        if self.audit_event is not None:
            event["audit"] = self.audit_event.to_dict()
        return event


__all__ = [
    "AUDIT_EVENT_TYPES",
    "SolverAuditEvent",
    "SolverAuditEventType",
    "SolverEvent",
]
