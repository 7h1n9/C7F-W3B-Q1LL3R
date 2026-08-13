"""Muteki intent routing and exclusive-resource dispatch semantics.

The upstream Muteki graph treats route, lane, and resource metadata as part of
the scheduling contract.  The local compatibility graph does not expose those
operations yet, so this module uses capability detection and remains a no-op
for that backend.  It keeps the policy at the Coordinator boundary instead of
embedding it in a challenge-specific strategy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .outcomes import stable_route_hash


@dataclass(frozen=True, slots=True)
class SemanticReservation:
    """Locks acquired for one dispatched intent."""

    worker_id: str
    intent_id: str
    route_hash: str = ""
    lane_key: str = ""
    resource_key: str = ""


@dataclass(frozen=True, slots=True)
class DispatchDecision:
    """Result of applying graph-backed dispatch constraints."""

    allowed: bool
    reason: str = ""
    reservation: SemanticReservation | None = None


def acquire_dispatch_semantics(
    graph: Any,
    *,
    worker_id: str,
    intent_id: str,
    payload: dict[str, Any] | None,
) -> DispatchDecision:
    """Validate and reserve official Muteki scheduling metadata.

    ``route_hash`` is checked first.  Exclusive lanes are then locked before a
    resource lock is acquired, so a failed resource reservation can safely
    release the lane.  Graphs without the upstream methods intentionally pass
    through; this preserves the current compatibility backend while allowing
    the feature-flagged upstream graph to enforce the stronger contract.
    """

    values = dict(payload or {})
    route_hash = str(values.get("route_hash") or "").strip()
    route_hash = stable_route_hash(values, intent_id=intent_id)
    lane_key = str(values.get("lane_key") or "").strip()
    resource_key = str(values.get("resource_key") or "").strip()
    risk_class = str(values.get("risk_class") or "").strip()

    if route_hash and hasattr(graph, "is_route_suppressed"):
        if graph.is_route_suppressed(route_hash):
            return DispatchDecision(False, "ROUTE_SUPPRESSED")

    route_acquired = False
    if route_hash and hasattr(graph, "try_claim_activity"):
        route_acquired = bool(
            graph.try_claim_activity(
                worker=worker_id,
                key=f"route:{route_hash}",
                lease_s=900.0,
            )
        )
        if not route_acquired:
            return DispatchDecision(False, "ROUTE_UNAVAILABLE")

    if hasattr(graph, "check_resource_conflicts"):
        conflict = graph.check_resource_conflicts(
            resource_key=resource_key,
            lane_key=lane_key,
            by_worker=worker_id,
        )
        if bool(conflict.get("conflict")):
            blockers = conflict.get("blockers") or []
            reason = str(blockers[0].get("kind") if blockers else "resource")
            if lane_key and hasattr(graph, "defer_intent_for_lane"):
                graph.defer_intent_for_lane(
                    actor="coordinator",
                    intent_id=intent_id,
                    lane_key=lane_key,
                    against_locked_seq=_locked_sequence(blockers),
                )
            if route_acquired and hasattr(graph, "release_activity"):
                graph.release_activity(worker=worker_id, key=f"route:{route_hash}")
            return DispatchDecision(False, f"SEMANTIC_CONFLICT:{reason}")

    lane_acquired = False
    if lane_key and hasattr(graph, "lock_lane"):
        lane_result = graph.lock_lane(
            actor="coordinator",
            lane_key=lane_key,
            risk_class=risk_class,
            owner_worker=worker_id,
            owner_intent=intent_id,
        )
        lane_acquired = bool(lane_result.get("acquired"))
        if not lane_acquired:
            if route_acquired and hasattr(graph, "release_activity"):
                graph.release_activity(worker=worker_id, key=f"route:{route_hash}")
            return DispatchDecision(False, "LANE_UNAVAILABLE")

    resource_acquired = False
    if resource_key and hasattr(graph, "request_resource_lock"):
        resource_result = graph.request_resource_lock(
            actor="coordinator",
            resource_key=resource_key,
            risk_class=risk_class,
            owner_worker=worker_id,
            owner_intent=intent_id,
        )
        resource_acquired = bool(resource_result.get("acquired"))
        if not resource_acquired:
            if lane_acquired:
                graph.release_lane(
                    actor="coordinator",
                    lane_key=lane_key,
                    by_worker=worker_id,
                )
            if route_acquired and hasattr(graph, "release_activity"):
                graph.release_activity(worker=worker_id, key=f"route:{route_hash}")
            return DispatchDecision(False, "RESOURCE_UNAVAILABLE")

    reservation = None
    if route_acquired or lane_acquired or resource_acquired:
        reservation = SemanticReservation(
            worker_id=worker_id,
            intent_id=intent_id,
            route_hash=route_hash if route_acquired else "",
            lane_key=lane_key if lane_acquired else "",
            resource_key=resource_key if resource_acquired else "",
        )
    return DispatchDecision(True, reservation=reservation)


def release_dispatch_semantics(graph: Any, reservation: SemanticReservation) -> None:
    """Release only the locks acquired for one completed worker."""

    if reservation.resource_key and hasattr(graph, "release_resource_lock"):
        graph.release_resource_lock(
            actor="coordinator",
            resource_key=reservation.resource_key,
            by_worker=reservation.worker_id,
        )
    if reservation.route_hash and hasattr(graph, "release_activity"):
        graph.release_activity(
            worker=reservation.worker_id,
            key=f"route:{reservation.route_hash}",
        )
    if reservation.lane_key and hasattr(graph, "release_lane"):
        graph.release_lane(
            actor="coordinator",
            lane_key=reservation.lane_key,
            by_worker=reservation.worker_id,
        )


def _locked_sequence(blockers: list[Any]) -> int:
    for blocker in blockers:
        if isinstance(blocker, dict):
            try:
                return int(blocker.get("locked_seq") or 0)
            except (TypeError, ValueError):
                continue
    return 0


__all__ = [
    "DispatchDecision",
    "SemanticReservation",
    "acquire_dispatch_semantics",
    "release_dispatch_semantics",
]
