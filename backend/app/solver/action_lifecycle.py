"""Durable action execution identity and recovery helpers for Solver v2."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .action import ActionIntent


class ActionExecutionState(StrEnum):
    PENDING = "PENDING"
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


def generate_fingerprint(action_name: str, parameters: Mapping[str, Any] | None = None) -> str:
    """Generate a stable identity for one logical action request."""

    canonical = json.dumps(
        {
            "action_name": str(action_name),
            "parameters": dict(parameters or {}),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ActionExecutionRecord:
    action_id: str
    action_name: str
    state: ActionExecutionState
    fingerprint: str
    retry_of: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_reason: str | None = None

    @classmethod
    def pending(
        cls,
        action: ActionIntent,
        *,
        fingerprint: str | None = None,
    ) -> "ActionExecutionRecord":
        return cls(
            action_id=action.action_id or uuid.uuid4().hex,
            action_name=action.action_name,
            state=ActionExecutionState.PENDING,
            fingerprint=fingerprint or generate_fingerprint(action.action_name, action.parameters),
            retry_of=action.retry_of,
        )

    def started(self, *, timestamp: datetime | None = None) -> "ActionExecutionRecord":
        return self._transition(ActionExecutionState.STARTED, timestamp=timestamp or _now())

    def completed(self, *, timestamp: datetime | None = None) -> "ActionExecutionRecord":
        return self._transition(ActionExecutionState.COMPLETED, timestamp=timestamp or _now())

    def failed(self, reason: str, *, timestamp: datetime | None = None) -> "ActionExecutionRecord":
        return self._transition(
            ActionExecutionState.FAILED,
            timestamp=timestamp or _now(),
            error_reason=str(reason),
        )

    def interrupted(self, reason: str, *, timestamp: datetime | None = None) -> "ActionExecutionRecord":
        return self._transition(
            ActionExecutionState.INTERRUPTED,
            timestamp=timestamp or _now(),
            error_reason=str(reason),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "action_name": self.action_name,
            "state": self.state.value,
            "fingerprint": self.fingerprint,
            "retry_of": self.retry_of,
            "started_at": _serialize_time(self.started_at),
            "completed_at": _serialize_time(self.completed_at),
            "error_reason": self.error_reason,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ActionExecutionRecord":
        return cls(
            action_id=str(value.get("action_id") or ""),
            action_name=str(value.get("action_name") or ""),
            state=ActionExecutionState(str(value.get("state") or ActionExecutionState.PENDING)),
            fingerprint=str(value.get("fingerprint") or ""),
            retry_of=str(value["retry_of"]) if value.get("retry_of") else None,
            started_at=_parse_time(value.get("started_at")),
            completed_at=_parse_time(value.get("completed_at")),
            error_reason=str(value["error_reason"]) if value.get("error_reason") else None,
        )

    def _transition(
        self,
        state: ActionExecutionState,
        *,
        timestamp: datetime,
        error_reason: str | None = None,
    ) -> "ActionExecutionRecord":
        return ActionExecutionRecord(
            action_id=self.action_id,
            action_name=self.action_name,
            state=state,
            fingerprint=self.fingerprint,
            retry_of=self.retry_of,
            started_at=self.started_at or timestamp,
            completed_at=timestamp,
            error_reason=error_reason,
        )


def validate_retry_relationship(
    record: ActionExecutionRecord,
    interrupted: ActionExecutionRecord | None,
) -> bool:
    """Return whether a retry explicitly and faithfully refers to an interruption."""

    if interrupted is None:
        return record.retry_of is None
    return (
        record.retry_of == interrupted.action_id
        and record.fingerprint == interrupted.fingerprint
    )


def find_interrupted_action(value: Any) -> ActionExecutionRecord | None:
    """Find an in-flight action from a Blackboard or its control mapping."""

    control = getattr(value, "control", value)
    if not isinstance(control, Mapping):
        return None
    active_action = control.get("active_action")
    if not isinstance(active_action, Mapping):
        return None
    try:
        record = ActionExecutionRecord.from_mapping(active_action)
    except (TypeError, ValueError):
        return None
    return record if record.state is ActionExecutionState.STARTED else None


def _now() -> datetime:
    return datetime.now(UTC)


def _serialize_time(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


__all__ = [
    "ActionExecutionRecord",
    "ActionExecutionState",
    "find_interrupted_action",
    "generate_fingerprint",
    "validate_retry_relationship",
]
