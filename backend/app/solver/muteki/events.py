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
    # One record per tool invocation, carrying the (redacted) command and a
    # bounded excerpt of its output.  Deliberately *not* coalesced like
    # WORKER_STEP: aggregation is what erased the command from the trace.
    WORKER_COMMAND = "worker_command"
    WORKER_FINISHED = "worker_finished"
    PHASE_CHANGED = "phase_changed"
    PREPARE_ENGINE_CHECKED = "prepare.engine.checked"
    # A failed engine's lane has been handed to a healthy one.  Recorded so the
    # event stream shows *why* a single-Worker round was carrying two lanes --
    # otherwise the degradation is invisible until the Run comes back unsolved.
    ENGINE_BACKFILL = "engine.backfill"
    # The curated FIRST ACTION was placed in a lane goal.  Recorded because the
    # Race Worker's goal is otherwise *not persisted anywhere* -- Race proposes no
    # Intent, so ``intent_proposed`` never carries it.  Without this event "the
    # Worker ignored the FIRST ACTION" and "the FIRST ACTION was never injected"
    # are indistinguishable from ``run_events``, which is exactly the ambiguity
    # that made the 2026-11-07 adoption audit need an out-of-band script.
    #
    # Payload: ``text`` (what was injected), ``chars``, ``source`` (entry id +
    # field), ``verdict`` (why this string was chosen), ``lane``, ``goal_chars``
    # and ``in_window`` (did the 600-char ``intent_goal`` cut keep it).
    EXPLOIT_FIRST_ACTION = "exploit.first_action"
    # The same recording, for the lower-authority probe channel: a recipe that
    # matched the box but was never measured here.
    #
    # Measured 2026-11-08: 26 of the 81 UNSOLVED Challenges now receive a concrete
    # first command, but only 2 of those come through ``exploit.first_action`` --
    # the other 24 arrive as probes.  Without a matching event, an adoption audit
    # would read those 24 Runs as "no first action was available", which is the
    # exact false negative the FIRST ACTION event was added to prevent.  Kept as a
    # distinct type rather than a field on the existing event so the two
    # authorities stay separable in a query.
    #
    # Payload: ``text``, ``chars``, ``lane``, ``goal_chars``, ``in_window``.
    EXPLOIT_PROBE_ACTION = "exploit.probe_action"
    REASON_STARTED = "reason.started"
    REASON_COMPLETED = "reason.completed"
    REASON_FAILED = "reason.failed"
    REASON_MODEL_SELECTED = "runtime.reason.model.selected"
    REASON_MODEL_FALLBACK = "runtime.reason.model.fallback"
    # The relay's content audit rejected the Reason prompt and the adapter is
    # retrying with softened prose.  Recorded separately from a fallback: the
    # model still answered, so a Run that shows this event has NOT lost its
    # planning quality.
    REASON_MODEL_CONTENT_POLICY = "runtime.reason.model.content_policy_retry"
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
