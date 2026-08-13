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
    REVIEW_FINDING = "review_finding"
    REVIEW_PROPOSAL = "review_proposal"
    BRANCH_SPLIT = "branch_split"
    BRANCH_RESOLVED = "branch_resolved"
    LANE_LOCKED = "lane_locked"
    LANE_RELEASED = "lane_released"
    INTENT_LANE_DEFERRED = "intent_lane_deferred"
    FACT_CHALLENGED = "fact_challenged"
    FACT_REVALIDATED = "fact_revalidated"
    FACT_REJECTED = "fact_rejected"
    FACT_MERGED = "fact_merged"
    FACT_SUPERSEDED = "fact_superseded"
    FACT_PINNED = "fact_pinned"
    ROUTE_SUPPRESSED = "route_suppressed"
    ROUTE_REOPENED = "route_reopened"
    REVIEW_PROPOSAL_DECISION = "review_proposal_decision"
    COORDINATOR_DIRECTIVE = "coordinator_directive"
    OPERATOR_DIRECTIVE = "operator_directive"
    OPERATOR_DIRECTIVE_STATUS = "operator_directive_status"
    HITL_CLASSIFIED = "hitl_classified"
    INTENT_STATE_CHANGED = "intent_state_changed"
    GRAPH_COMPACTED = "graph_compacted"
    WORKER_STARTED = "worker_started"
    WORKER_STEP = "worker_step"
    WORKER_FINISHED = "worker_finished"
    PHASE_CHANGED = "phase_changed"
    PREPARE_ENGINE_CHECKED = "prepare.engine.checked"
    REASON_STARTED = "reason.started"
    REASON_COMPLETED = "reason.completed"
    REASON_FAILED = "reason.failed"
    REASON_MODEL_SELECTED = "runtime.reason.model.selected"
    REASON_MODEL_FALLBACK = "runtime.reason.model.fallback"
    REASON_MODEL_REACTIVATED = "runtime.reason.model.reactivated"
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
