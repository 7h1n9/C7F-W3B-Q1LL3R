"""Projection of official Muteki events and intents into current contracts.

The upstream graph is intentionally kept behind this adapter.  Callers can
replay official graph events through the existing ``EventBridge`` without
making the current database event service depend on upstream implementation
details.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from ..events import EventEnvelope, EventType

_EVENT_TYPE_MAP = {
    "fact_added": EventType.FACT_ADDED,
    "dead_end": EventType.DEAD_END,
    "intent_proposed": EventType.INTENT_PROPOSED,
    "intent_claimed": EventType.INTENT_CLAIMED,
    "intent_concluded": EventType.INTENT_CONCLUDED,
    "flag_found": EventType.FLAG_FOUND,
    "poc_saved": EventType.POC_SAVED,
    "resource_locked": EventType.RESOURCE_LOCKED,
    "resource_released": EventType.RESOURCE_RELEASED,
    "review_finding": EventType.REVIEW_FINDING,
    "review_proposal": EventType.REVIEW_PROPOSAL,
    "branch_split": EventType.BRANCH_SPLIT,
    "branch_resolved": EventType.BRANCH_RESOLVED,
    "lane_locked": EventType.LANE_LOCKED,
    "lane_released": EventType.LANE_RELEASED,
    "intent_lane_deferred": EventType.INTENT_LANE_DEFERRED,
    "fact_challenged": EventType.FACT_CHALLENGED,
    "fact_revalidated": EventType.FACT_REVALIDATED,
    "fact_rejected": EventType.FACT_REJECTED,
    "fact_merged": EventType.FACT_MERGED,
    "fact_superseded": EventType.FACT_SUPERSEDED,
    "fact_pinned": EventType.FACT_PINNED,
    "route_suppressed": EventType.ROUTE_SUPPRESSED,
    "route_reopened": EventType.ROUTE_REOPENED,
    "review_proposal_decision": EventType.REVIEW_PROPOSAL_DECISION,
    "coordinator_directive": EventType.COORDINATOR_DIRECTIVE,
    "operator_directive": EventType.OPERATOR_DIRECTIVE,
    "operator_directive_status": EventType.OPERATOR_DIRECTIVE_STATUS,
    "hitl_classified": EventType.HITL_CLASSIFIED,
    "intent_state_changed": EventType.INTENT_STATE_CHANGED,
    "graph_compacted": EventType.GRAPH_COMPACTED,
}

_SENSITIVE_KEYS = frozenset(
    {
        "raw",
        "raw_result",
        "response",
        "body",
        "cookie",
        "token",
        "secret",
        "password",
        "ground_truth",
    }
)


class UpstreamIntentAdapter:
    """Map current intent terminology to official SharedGraph calls."""

    @staticmethod
    def propose(
        graph: Any,
        *,
        actor: str,
        intent_id: str,
        description: str,
        payload: Mapping[str, Any] | None = None,
    ) -> int:
        """Persist a current-style intent through the official graph API."""

        return int(
            graph.propose_intent(
                actor=actor,
                intent_id=intent_id,
                goal=description,
                payload=dict(payload or {}),
            )
        )

    @staticmethod
    def claim(graph: Any, *, worker: str, intent_id: str, lease_s: float = 300.0) -> bool:
        """Claim an official intent using the current worker terminology."""

        return bool(graph.claim_intent(worker=worker, intent_id=intent_id, lease_s=lease_s))

    @staticmethod
    def conclude(graph: Any, *, actor: str, intent_id: str, result: str = "") -> int:
        """Conclude an official intent and return its event sequence."""

        return int(graph.conclude_intent(actor=actor, intent_id=intent_id, result=result))


def project_upstream_event(
    event: Mapping[str, Any],
    *,
    challenge_id: str,
) -> EventEnvelope:
    """Convert one official SharedGraph event into a current EventEnvelope."""

    kind = str(event.get("kind") or "unknown")
    event_type = _EVENT_TYPE_MAP.get(kind, f"upstream.{kind}")
    timestamp = _timestamp(event.get("ts"))
    projected_payload = _safe_value(event.get("payload") or {})
    if event.get("artifact_id"):
        projected_payload = {
            **projected_payload,
            "evidence_refs": [str(event["artifact_id"])],
        }
    return EventEnvelope(
        sequence=int(event.get("seq") or 0),
        timestamp=timestamp,
        challenge_id=str(challenge_id),
        actor=str(event.get("actor") or "upstream"),
        event_type=str(event_type),
        payload={
            "upstream_sequence": int(event.get("seq") or 0),
            "upstream_event_type": kind,
            "payload": projected_payload,
        },
        verified=bool(event.get("verified", False)),
        confidence=float(event.get("confidence", 1.0) or 0.0),
    )


def project_upstream_events(
    events: list[Mapping[str, Any]],
    *,
    challenge_id: str,
) -> list[EventEnvelope]:
    """Project official events in sequence order for deterministic replay."""

    return [
        project_upstream_event(event, challenge_id=challenge_id)
        for event in sorted(events, key=lambda item: int(item.get("seq") or 0))
    ]


def _timestamp(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value), UTC).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return datetime.now(UTC).isoformat()


def _safe_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _safe_value(item)
            for key, item in value.items()
            if str(key).casefold() not in _SENSITIVE_KEYS
        }
    if isinstance(value, list):
        return [_safe_value(item) for item in value]
    if isinstance(value, tuple):
        return [_safe_value(item) for item in value]
    return value


__all__ = [
    "UpstreamIntentAdapter",
    "project_upstream_event",
    "project_upstream_events",
]
