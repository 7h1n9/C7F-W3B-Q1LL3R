from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    FACT_ADDED = "fact_added"
    DEAD_END = "dead_end"
    INTENT_PROPOSED = "intent_proposed"
    INTENT_CLAIMED = "intent_claimed"
    INTENT_RELEASED = "intent_released"
    INTENT_CONCLUDED = "intent_concluded"
    FLAG_CANDIDATE = "flag_candidate"
    FLAG_FOUND = "flag_found"
    POC_SAVED = "poc_saved"
    RESOURCE_LOCKED = "resource_locked"
    RESOURCE_RELEASED = "resource_released"
    WORKER_STARTED = "worker_started"
    WORKER_FINISHED = "worker_finished"
    PHASE_CHANGED = "phase_changed"
    PREPARE_ENGINE_CHECKED = "prepare.engine.checked"
    RUN_FINISHED = "run_finished"


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    sequence: int
    timestamp: str
    challenge_id: str
    actor: str
    event_type: str
    payload: dict[str, Any]
    verified: bool = False
    confidence: float = 1.0

    @classmethod
    def now(
        cls,
        sequence: int,
        *,
        challenge_id: str,
        actor: str,
        event_type: EventType | str,
        payload: dict[str, Any] | None = None,
        verified: bool = False,
        confidence: float = 1.0,
    ) -> "EventEnvelope":
        return cls(
            sequence,
            datetime.now(UTC).isoformat(),
            challenge_id,
            actor,
            str(event_type),
            dict(payload or {}),
            bool(verified),
            float(confidence),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "challenge_id": self.challenge_id,
            "actor": self.actor,
            "event_type": self.event_type,
            "payload": dict(self.payload),
            "verified": self.verified,
            "confidence": self.confidence,
        }
