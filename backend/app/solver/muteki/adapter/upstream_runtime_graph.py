"""Production-compatible facade over the official Muteki SharedGraph.

The current coordinator was written against the project's original graph
surface.  This facade keeps that surface stable while making the official
SQLiteSharedGraph the state authority when explicitly selected.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from ..events import EventEnvelope
from ..graph import DeadEnd, Fact, Flag, Intent, MutekiGraph
from ..upstream_bridge import to_upstream_challenge
from .upstream_events import project_upstream_events


def runtime_graph_backend(default: str = "current") -> str:
    """Return the selected graph backend without changing test/compat defaults.

    The compatibility graph remains the library default.  Production Muteki
    runtime may pass ``default="upstream"`` so the official SQLite
    SharedGraph is selected when deployment has not supplied an override.
    Keeping the default explicit preserves callers that intentionally use the
    compatibility graph for isolated tests or legacy adapters.
    """

    value = (
        os.getenv("APP_MUTEKI_GRAPH_BACKEND")
        or os.getenv("MUTEKI_GRAPH_BACKEND")
        or default
    )
    normalized = value.strip().casefold()
    if normalized not in {"current", "upstream"}:
        raise ValueError(f"Unsupported Muteki graph backend: {value}")
    return normalized


def create_runtime_graph(
    db_path: str | Path,
    *,
    challenge: Any,
    challenge_id: str,
    event_subscriber: Callable[[EventEnvelope], None] | None = None,
    backend: str | None = None,
) -> MutekiGraph | "UpstreamRuntimeGraph":
    """Create the selected graph facade.

    ``backend`` is an additive production override.  When omitted, the
    historical environment/default selection remains unchanged.
    """

    selected_backend = runtime_graph_backend() if backend is None else runtime_graph_backend(backend)
    if selected_backend == "upstream":
        graph: MutekiGraph | UpstreamRuntimeGraph = UpstreamRuntimeGraph(
            db_path,
            challenge=challenge,
            challenge_id=challenge_id,
            event_subscriber=event_subscriber,
        )
    else:
        graph = MutekiGraph(
            db_path,
            challenge_id=challenge_id,
            event_subscriber=event_subscriber,
        )
    _use_container_safe_journal_mode(graph)
    return graph


def _use_container_safe_journal_mode(graph: Any) -> None:
    """Keep the worker-visible graph DB readable across the Docker bind mount.

    SQLite WAL sidecars are not reliably shared across Docker Desktop bind
    mounts.  Worker containers observed ``disk I/O error`` and
    ``no such table: events`` when opening the live WAL graph.  DELETE mode
    keeps the graph in one file that both the host process and the worker
    container can open; short writes remain serialized by SQLite's busy
    timeout.
    """

    connection = getattr(getattr(graph, "_graph", None), "_conn", None)
    if connection is None:
        connection = getattr(graph, "_db", None)
    if connection is None:
        return
    try:
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
    except sqlite3.Error:
        # A read-only or externally locked graph keeps its existing mode.
        pass


class UpstreamRuntimeGraph:
    """Adapt official SharedGraph operations to the current Muteki surface."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        challenge: Any,
        challenge_id: str,
        event_subscriber: Callable[[EventEnvelope], None] | None = None,
    ) -> None:
        from muteki.swarm.shared_graph import SQLiteSharedGraph

        self.db_path = Path(db_path)
        self.challenge_id = str(challenge_id)
        self._challenge = challenge
        self._subscriber = event_subscriber
        self._graph = SQLiteSharedGraph.open(
            db_path=self.db_path,
            challenge=to_upstream_challenge(challenge),
        )
        self._last_upstream_sequence = 0
        self._last_review_proposal_sequence = 0
        self._local_sequence = 0

    def close(self) -> None:
        self._graph.close()

    def native_graph(self) -> Any:
        """Return the official SharedGraph for native Worker/Sandbox adapters.

        The facade remains the production contract for the Coordinator. This
        explicit capability prevents a native upstream Worker from accidentally
        opening the compatibility graph schema when the feature flag is
        misconfigured.
        """

        return self._graph

    def _sync_events(self) -> None:
        events = self._graph.events_since(self._last_upstream_sequence)
        if not events:
            return
        self._last_upstream_sequence = max(
            self._last_upstream_sequence,
            max(int(event.get("seq") or 0) for event in events),
        )
        projected = project_upstream_events(events, challenge_id=self.challenge_id)
        for event in projected:
            self._local_sequence = max(self._local_sequence, event.sequence)
            if self._subscriber is not None:
                self._subscriber(event)

    def _emit_local(self, *, actor: str, event_type: str, payload: dict[str, Any]) -> int:
        self._local_sequence = max(self._local_sequence, self._last_upstream_sequence) + 1
        event = EventEnvelope(
            sequence=self._local_sequence,
            timestamp=datetime.now(UTC).isoformat(),
            challenge_id=self.challenge_id,
            actor=actor,
            event_type=str(event_type),
            payload=dict(payload),
        )
        if self._subscriber is not None:
            self._subscriber(event)
        return event.sequence

    def emit_event(self, *, actor: str, event_type: str, payload: dict[str, Any] | None = None) -> int:
        return self._emit_local(actor=actor, event_type=str(event_type), payload=dict(payload or {}))

    def add_fact(
        self,
        *,
        actor: str,
        content: str,
        verified: bool = False,
        evidence_refs: list[str] | tuple[str, ...] = (),
        dedupe_key: str | None = None,
    ) -> int:
        del dedupe_key  # official graph owns fact identity and deduplication
        sequence = self._graph.add_evidence(
            actor=actor,
            source=actor,
            fact=content,
            artifact_id=str(evidence_refs[0]) if evidence_refs else None,
            verified=verified,
            confidence=1.0 if verified else 0.5,
        )
        self._sync_events()
        return int(sequence)

    def add_dead_end(self, *, actor: str, description: str) -> int:
        sequence = self._graph.add_dead_end(actor=actor, reason=description)
        self._sync_events()
        return int(sequence)

    def propose_intent(
        self,
        *,
        actor: str,
        intent_id: str | None = None,
        description: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        resolved_id = intent_id or f"intent_{uuid.uuid4().hex}"
        self._graph.propose_intent(
            actor=actor,
            intent_id=resolved_id,
            goal=description,
            payload=dict(payload or {}),
        )
        self._sync_events()
        return resolved_id

    def claim_intent(self, *, worker: str, intent_id: str, lease_s: float = 300.0) -> bool:
        claimed = bool(self._graph.claim_intent(worker=worker, intent_id=intent_id, lease_s=lease_s))
        self._sync_events()
        return claimed

    def record_engine_attempt(self, *, intent_id: str, engine_attempt_id: str, actor: str) -> bool:
        """Persist assignment identity in the existing Intent event envelope."""

        connection = getattr(self._graph, "_conn", None)
        lock = getattr(self._graph, "_lock", None)
        if connection is None or lock is None or not intent_id or not engine_attempt_id:
            return False
        with lock:
            row = connection.execute(
                "SELECT created_seq FROM intents WHERE intent_id=? AND challenge_id=?",
                (str(intent_id), self._graph.challenge.id),
            ).fetchone()
            if not row:
                return False
            event = connection.execute(
                "SELECT payload FROM events WHERE seq=?",
                (int(row[0]),),
            ).fetchone()
            if not event:
                return False
            try:
                payload = json.loads(event[0] or "{}")
            except (TypeError, ValueError):
                payload = {}
            payload["engine_attempt_id"] = str(engine_attempt_id)[:240]
            connection.execute(
                "UPDATE events SET payload=? WHERE seq=?",
                (json.dumps(payload, ensure_ascii=False, default=str), int(row[0])),
            )
            connection.commit()
        self._emit_local(
            actor=actor,
            event_type="intent_engine_assigned",
            payload={"intent_id": str(intent_id), "engine_attempt_id": str(engine_attempt_id)[:240]},
        )
        return True

    def conclude_intent(self, *, actor: str, intent_id: str, result: str = "") -> bool:
        sequence = int(self._graph.conclude_intent(actor=actor, intent_id=intent_id, result=result))
        self._sync_events()
        return sequence > 0

    def release_intent(self, *, worker: str, intent_id: str) -> bool:
        """Release one failed local claim through the official SQLite state."""

        connection = getattr(self._graph, "_conn", None)
        lock = getattr(self._graph, "_lock", None)
        if connection is None or lock is None:
            return False
        with lock:
            cursor = connection.execute(
                "UPDATE intents SET status='open', worker=NULL, lease_until=NULL "
                "WHERE intent_id=? AND challenge_id=? AND status='claimed' AND worker=?",
                (str(intent_id), self._graph.challenge.id, str(worker)),
            )
            connection.commit()
        if cursor.rowcount:
            self._emit_local(
                actor=worker,
                event_type="intent_released",
                payload={"intent_id": str(intent_id)},
            )
        return cursor.rowcount == 1

    def release_claims(self, *, actor: str) -> None:
        self._graph.release_claims_for_finalize(reason=f"finalize:{actor}")
        self._sync_events()

    # Official Muteki scheduling/review semantics.  These methods deliberately
    # stay as a thin facade: the upstream SQLiteSharedGraph remains the owner of
    # route, branch, lane, and resource state.
    def is_route_suppressed(self, route_hash: str) -> bool:
        return bool(self._graph.is_route_suppressed(route_hash))

    def suppress_route(self, **kwargs: Any) -> dict[str, Any]:
        result = self._graph.suppress_route(**kwargs)
        self._sync_events()
        return dict(result or {})

    def reopen_route(self, **kwargs: Any) -> dict[str, Any]:
        result = self._graph.reopen_route(**kwargs)
        self._sync_events()
        return dict(result or {})

    def split_branch(self, **kwargs: Any) -> dict[str, Any]:
        result = self._graph.split_branch(**kwargs)
        self._sync_events()
        return dict(result or {})

    def resolve_branch(self, **kwargs: Any) -> dict[str, Any]:
        result = self._graph.resolve_branch(**kwargs)
        self._sync_events()
        return dict(result or {})

    def lock_lane(self, **kwargs: Any) -> dict[str, Any]:
        result = self._graph.lock_lane(**kwargs)
        self._sync_events()
        return dict(result or {})

    def release_lane(self, **kwargs: Any) -> dict[str, Any]:
        result = self._graph.release_lane(**kwargs)
        self._sync_events()
        return dict(result or {})

    def defer_intent_for_lane(self, **kwargs: Any) -> int:
        sequence = int(self._graph.defer_intent_for_lane(**kwargs))
        self._sync_events()
        return sequence

    def active_lanes(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._graph.active_lanes()]

    def try_claim_activity(self, **kwargs: Any) -> bool:
        claimed = bool(self._graph.try_claim_activity(**kwargs))
        self._sync_events()
        return claimed

    def release_activity(self, **kwargs: Any) -> None:
        self._graph.release_activity(**kwargs)
        self._sync_events()

    def request_resource_lock(self, **kwargs: Any) -> dict[str, Any]:
        result = self._graph.request_resource_lock(**kwargs)
        self._sync_events()
        return dict(result or {})

    def release_resource_lock(self, **kwargs: Any) -> dict[str, Any]:
        result = self._graph.release_resource_lock(**kwargs)
        self._sync_events()
        return dict(result or {})

    def active_resource_locks(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._graph.active_resource_locks()]

    def check_resource_conflicts(self, **kwargs: Any) -> dict[str, Any]:
        return dict(self._graph.check_resource_conflicts(**kwargs) or {})

    def add_review_finding(self, **kwargs: Any) -> int:
        sequence = int(self._graph.add_review_finding(**kwargs))
        self._sync_events()
        return sequence

    def add_review_proposal(self, **kwargs: Any) -> int:
        sequence = int(self._graph.add_review_proposal(**kwargs))
        self._sync_events()
        return sequence

    def apply_review_proposals(self, *, actor: str = "coordinator") -> list[dict[str, Any]]:
        """Apply the bounded official tier-2 review decisions.

        The vendored Muteki Coordinator accepts ``ROUTE_SUPPRESS`` only after
        three genuine failures and a confidence of at least 0.80.  Keeping the
        decision in this facade lets the compatibility Coordinator use the
        same upstream rule without reaching into the native graph object from
        production code.
        """

        proposals = self._graph.events_since(
            self._last_review_proposal_sequence,
            kinds=["review_proposal"],
        )
        decisions: list[dict[str, Any]] = []
        for event in proposals:
            sequence = int(event.get("seq") or 0)
            self._last_review_proposal_sequence = max(
                self._last_review_proposal_sequence,
                sequence,
            )
            envelope = dict(event.get("payload") or {})
            marker = str(envelope.get("marker") or "").upper()
            tier = str(envelope.get("tier") or "tier1")
            payload = dict(envelope.get("payload") or {})
            accepted = False
            reason = ""
            applied_seq: int | None = None
            try:
                if tier == "tier2" and marker == "ROUTE_SUPPRESS":
                    route_hash = str(payload.get("route_hash") or "")
                    failures = int(self._graph.genuine_failures_for_route(route_hash))
                    try:
                        confidence = float(payload.get("confidence", 1.0) or 1.0)
                    except (TypeError, ValueError):
                        confidence = 0.0
                    accepted = failures >= 3 and confidence >= 0.80
                    reason = f"failures={failures}, confidence={confidence:.2f}"
                    if accepted:
                        result = self._graph.suppress_route(
                            actor=actor,
                            route_hash=route_hash,
                            label=str(payload.get("label") or ""),
                            reason=str(payload.get("reason") or ""),
                            until=str(payload.get("until") or "new_evidence"),
                            matching_intents=[
                                str(item)
                                for item in payload.get("matching_intents", [])
                                if item
                            ],
                        )
                        applied_seq = int(result.get("seq") or 0) or None
                else:
                    reason = f"unsupported marker {marker}"
                decision = "accepted" if accepted else "deferred"
                self._graph.decide_review_proposal(
                    actor=actor,
                    proposal_seq=sequence,
                    decision=decision,
                    reason=reason,
                    applied_seq=applied_seq,
                )
                decisions.append({
                    "proposal_seq": sequence,
                    "marker": marker,
                    "decision": decision,
                    "reason": reason,
                    "applied_seq": applied_seq,
                })
            except Exception as error:
                reason = str(error)[:500]
                try:
                    self._graph.decide_review_proposal(
                        actor=actor,
                        proposal_seq=sequence,
                        decision="rejected",
                        reason=reason,
                    )
                except Exception:
                    pass
                decisions.append({
                    "proposal_seq": sequence,
                    "marker": marker,
                    "decision": "rejected",
                    "reason": reason,
                    "applied_seq": None,
                })
        self._sync_events()
        return decisions

    def decide_review_proposal(self, **kwargs: Any) -> int:
        sequence = int(self._graph.decide_review_proposal(**kwargs))
        self._sync_events()
        return sequence

    def to_review_summary(self) -> str:
        return str(self._graph.to_review_summary())

    def revision(self) -> int:
        # Native Workers use a separate SQLite connection/process. Refresh the
        # append-only event cursor before Coordinator compares revisions, or a
        # completed Worker write can be invisible for the next tick and consume
        # the remaining budget in the no-progress branch.
        self._sync_events()
        return int(self._last_upstream_sequence)

    def native_event_cursor(self) -> int:
        """Return the latest official event sequence, including other processes.

        Native Workers open the same SQLite graph in a separate process.  The
        facade's projected cursor is therefore not sufficient as a pre-worker
        checkpoint; query the official append-only event log at this boundary.
        """

        events = self._graph.events_since(0)
        return max((int(event.get("seq") or 0) for event in events), default=0)

    def native_fact_events_since(self, after: int = 0) -> list[dict[str, Any]]:
        """Read only new official fact events for the native Worker adapter."""

        return [
            dict(event)
            for event in self._graph.events_since(
                max(0, int(after)),
                kinds=["fact_added"],
            )
        ]

    def native_dead_end_events_since(self, after: int = 0) -> list[dict[str, Any]]:
        """Read explicit native Muteki route dead-end events for one Worker."""

        return [
            dict(event)
            for event in self._graph.events_since(
                max(0, int(after)),
                kinds=["dead_end"],
            )
        ]

    def add_native_fact(
        self,
        *,
        actor: str,
        content: str,
        evidence_refs: list[str] | tuple[str, ...] = (),
        intent_id: str | None = None,
    ) -> int:
        """Persist a safe Coordinator projection as an official intent product."""

        refs = tuple(str(ref) for ref in evidence_refs if str(ref))
        sequence = self._graph.add_evidence(
            actor=actor,
            source=actor,
            fact=content,
            artifact_id=refs[0] if refs else None,
            verified=False,
            confidence=0.5,
            intent_id=str(intent_id or "") or None,
        )
        self._sync_events()
        return int(sequence)

    def write_flag(self, *, actor: str, flag: str, real_output: str) -> int:
        from muteki.solver.gate import flag_ok

        accepted = flag_ok(
            str(flag).strip(),
            str(real_output),
            flag_format=str(getattr(self._challenge, "flag_pattern", r"flag\{[^}]+\}")),
            artifacts=None,
        )
        if not accepted:
            return self.add_dead_end(actor=actor, description="FLAG_REJECTED_BY_UPSTREAM_GATE")
        sequence = self._graph.flag_found(actor=actor, flag=str(flag).strip())
        self._sync_events()
        return int(sequence)

    def facts(self, *, verified_only: bool = False) -> list[Fact]:
        facts: list[Fact] = []
        for index, evidence in enumerate(self._graph.snapshot().evidence, start=1):
            if verified_only and not evidence.verified:
                continue
            refs = (str(evidence.artifact_id),) if evidence.artifact_id else ()
            facts.append(
                Fact(
                    fact_id=index,
                    content=evidence.fact,
                    source_worker_id=evidence.source_solver or evidence.source,
                    verified=bool(evidence.verified),
                    created_at="",
                    evidence_refs=refs,
                )
            )
        return facts

    def dead_ends(self) -> list[DeadEnd]:
        snapshot = self._graph.snapshot()
        return [DeadEnd(index, reason, "upstream", "") for index, reason in enumerate(snapshot.dead_ends, 1)]

    def intents(self, *, status: str | None = None) -> list[Intent]:
        items: dict[str, Intent] = {}
        for event in self._graph.events():
            payload = dict(event.get("payload") or {})
            intent_id = str(payload.get("intent_id") or "")
            if not intent_id:
                continue
            current = items.get(intent_id)
            kind = str(event.get("kind") or "")
            if kind == "intent_proposed":
                items[intent_id] = Intent(
                    intent_id,
                    str(payload.get("goal") or ""),
                    "open",
                    None,
                    _event_timestamp(event),
                    payload=dict(payload),
                    route_hash=str(payload.get("route_hash") or ""),
                    branch_id=str(payload.get("branch_id") or ""),
                    engine_attempt_id=str(payload.get("engine_attempt_id") or ""),
                )
            elif current is not None and kind == "intent_claimed":
                items[intent_id] = Intent(
                    current.intent_id,
                    current.description,
                    "claimed",
                    str(event.get("actor") or ""),
                    current.created_at,
                    current.lease_until,
                    current.payload,
                    current.route_hash,
                    current.branch_id,
                    current.engine_attempt_id,
                )
            elif current is not None and kind == "intent_concluded":
                items[intent_id] = Intent(
                    current.intent_id,
                    current.description,
                    "done",
                    current.claimed_by,
                    current.created_at,
                    current.lease_until,
                    current.payload,
                    current.route_hash,
                    current.branch_id,
                    current.engine_attempt_id,
                )
            elif current is not None and kind == "intent_engine_assigned":
                attempt_id = str(payload.get("engine_attempt_id") or current.engine_attempt_id)
                current_payload = dict(current.payload or {})
                current_payload["engine_attempt_id"] = attempt_id
                items[intent_id] = Intent(
                    current.intent_id,
                    current.description,
                    current.status,
                    current.claimed_by,
                    current.created_at,
                    current.lease_until,
                    current_payload,
                    current.route_hash,
                    current.branch_id,
                    attempt_id,
                )
        values = list(items.values())
        return [item for item in values if status is None or item.status == status]

    def dispatchable_intents(self) -> list[Intent]:
        """Return intents eligible for the next Worker claim.

        The upstream Swarm does not treat a claimed intent as permanently
        unavailable.  Its dispatch queue includes open intents and claimed
        intents whose lease has expired, then lets ``claim_intent`` perform the
        atomic ownership check.  Keep that same lease loop at this facade
        boundary; the official SQLite graph remains the state authority.
        """

        connection = getattr(self._graph, "_conn", None)
        lock = getattr(self._graph, "_lock", None)
        if connection is None or lock is None:
            return self.intents(status="open")

        now = time.time()
        with lock:
            rows = connection.execute(
                "SELECT intent_id FROM intents "
                "WHERE challenge_id=? AND dispatch_state='active' "
                "AND (status='open' OR (status='claimed' "
                "AND lease_until IS NOT NULL AND lease_until < ?)) "
                "ORDER BY priority DESC, created_seq",
                (self._graph.challenge.id, now),
            ).fetchall()

        current = {item.intent_id: item for item in self.intents()}
        return [
            replace(current[str(row[0])], status="open", claimed_by=None, lease_until=None)
            for row in rows
            if str(row[0]) in current
        ]

    def flags(self, *, verified_only: bool = False) -> list[Flag]:
        if verified_only is False:
            verified_only = True  # official graph only exposes gated flags here
        flags: list[Flag] = []
        for event in self._graph.events():
            if event.get("kind") != "flag_found":
                continue
            payload = dict(event.get("payload") or {})
            flags.append(
                Flag(
                    int(event.get("seq") or 0),
                    str(payload.get("flag") or ""),
                    str(event.get("actor") or "upstream"),
                    True,
                    _event_timestamp(event),
                )
            )
        return flags

    def snapshot(self) -> dict[str, Any]:
        pocs_method = getattr(self._graph, "pocs", None)
        pocs = list(pocs_method(inheritable_only=False)) if callable(pocs_method) else []
        return {
            "challenge_id": self.challenge_id,
            "revision": self._last_upstream_sequence,
            "facts": [
                {
                    "fact_id": fact.fact_id,
                    "content": fact.content,
                    "source_worker_id": fact.source_worker_id,
                    "verified": fact.verified,
                    "created_at": fact.created_at,
                    "evidence_refs": list(fact.evidence_refs),
                }
                for fact in self.facts()
            ],
            "dead_ends": [
                {
                    "dead_end_id": item.dead_end_id,
                    "description": item.description,
                    "source_worker_id": item.source_worker_id,
                }
                for item in self.dead_ends()
            ],
            "intents": [
                {
                    "intent_id": item.intent_id,
                    "description": item.description,
                    "status": item.status,
                    "claimed_by": item.claimed_by,
                }
                for item in self.intents()
            ],
            "flags": [
                {
                    "flag_id": item.flag_id,
                    "flag_value": item.flag_value,
                    "verified_by_gate": item.verified_by_gate,
                }
                for item in self.flags(verified_only=True)
            ],
            "pocs": pocs,
            "resources": [],
        }

    def to_reason_summary(self, standing_guidance: list[str] | None = None) -> str:
        """Expose the official SharedGraph planner view to Coordinator Reason."""

        return str(self._graph.to_reason_summary(standing_guidance=standing_guidance))

    def fact_pin_context(self, limit: int = 240) -> str:
        """Expose the official fact-retention index without duplicating it."""

        return str(self._graph.fact_pin_context(limit=limit))

    def events_since(self, after: int = 0) -> list[EventEnvelope]:
        return project_upstream_events(
            self._graph.events_since(max(0, int(after))),
            challenge_id=self.challenge_id,
        )


def _event_timestamp(event: dict[str, Any]) -> str:
    try:
        return datetime.fromtimestamp(float(event.get("ts")), UTC).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


__all__ = ["UpstreamRuntimeGraph", "create_runtime_graph", "runtime_graph_backend"]
