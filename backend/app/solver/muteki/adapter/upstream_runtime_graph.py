"""Production-compatible facade over the official Muteki SharedGraph.

The current coordinator was written against the project's original graph
surface.  This facade keeps that surface stable while making the official
SQLiteSharedGraph the state authority when explicitly selected.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from ..events import EventEnvelope
from ..gate import DEFAULT_FLAG_PATTERN, compile_flag_pattern
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
            flag_pattern=str(getattr(challenge, "flag_pattern", "") or "") or None,
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
        # Re-entrancy guard: harvesting appends an official ``flag_found``
        # event, which would otherwise be re-scanned by the next sync pass.
        self._harvesting_flags = False

    @property
    def flag_pattern(self) -> str:
        """Return the challenge flag shape that both gates must agree on."""

        return str(getattr(self._challenge, "flag_pattern", "") or "") or DEFAULT_FLAG_PATTERN

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
        if self._harvest_flags(events):
            # The gate approved a Worker-claimed flag and appended a
            # ``flag_found`` event; re-read the tail so the subscriber sees the
            # authoritative flag in this same synchronisation pass.
            tail = self._graph.events_since(self._last_upstream_sequence)
            if tail:
                self._last_upstream_sequence = max(
                    self._last_upstream_sequence,
                    max(int(event.get("seq") or 0) for event in tail),
                )
                events = [*events, *tail]
        projected = project_upstream_events(events, challenge_id=self.challenge_id)
        for event in projected:
            self._local_sequence = max(self._local_sequence, event.sequence)
            if self._subscriber is not None:
                self._subscriber(event)

    _FLAG_CLAIM_RE = re.compile(r"(?:FOUND_FLAG|FLAG)\s*=\s*([^\s'\"`]+)")

    def _harvest_flags(self, events: list[dict[str, Any]]) -> bool:
        """Promote a Worker-claimed flag to the official flag gate.

        The native Worker records the recovered candidate as prose inside a
        fact (``FOUND_FLAG=CTF2{...}``); the platform used to ignore that text
        entirely, so a Run that really had the flag still finished "unsolved".
        This only *submits* the candidate: the official ``flag_ok`` gate still
        decides, requiring the exact token to appear in the recorded output and
        to match the challenge format, so a hallucinated candidate can never
        become a verified flag.
        """

        if self._harvesting_flags:
            return False
        token_mode = self.flag_pattern.strip().casefold() == _TOKEN_FLAG_FORMAT
        matcher = None if token_mode else compile_flag_pattern(self.flag_pattern)
        submitted = False
        self._harvesting_flags = True
        try:
            for event in events:
                kind = str(event.get("kind") or "")
                if kind in {"flag_found", "dead_end"}:
                    continue
                text = _event_text(event.get("payload"))
                if not text:
                    continue
                candidates = [match.group(1) for match in self._FLAG_CLAIM_RE.finditer(text)]
                if not candidates and matcher is not None:
                    candidates = [match.group(0) for match in matcher.finditer(text)]
                actor = str(event.get("actor") or "upstream")
                for candidate in candidates:
                    flag = str(candidate).strip().strip(_FLAG_TRAILING_PUNCTUATION)
                    if not _looks_like_flag(flag, matcher, token_mode=token_mode):
                        continue
                    if self._submit_flag_to_gate(actor=actor, flag=flag, real_output=text) is not None:
                        submitted = True
                        break
        finally:
            self._harvesting_flags = False
        return submitted

    def _submit_flag_to_gate(self, *, actor: str, flag: str, real_output: str) -> int | None:
        """Submit one candidate to the official gate; return its seq or None."""

        from muteki.solver.gate import flag_ok

        accepted = flag_ok(
            str(flag).strip(),
            str(real_output),
            flag_format=self.flag_pattern,
            artifacts=None,
        )
        if not accepted:
            return None
        return int(self._graph.flag_found(actor=actor, flag=str(flag).strip()))

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
        sequence = self._submit_flag_to_gate(actor=actor, flag=flag, real_output=real_output)
        if sequence is None:
            return self.add_dead_end(actor=actor, description="FLAG_REJECTED_BY_UPSTREAM_GATE")
        self._sync_events()
        return sequence

    def record_flag_decision(self, *, actor: str, decision, evidence: str = "") -> int:
        """Record a flag claim so a refused value stays reviewable.

        ``write_flag`` can only *accept*: a value its gate refuses becomes a dead
        end, so a Worker that printed the right value under the wrong banner
        leaves no trace at all.  That is the shape the real-class boxes reached
        (``n1book{...}`` written by the box itself), and losing the value meant
        the next Run had to re-derive it from scratch.

        The value is written through the upstream graph's own ``flag_found``,
        which does **not** validate -- validation belongs to the read side, where
        ``flags()`` keeps only what matches the declared banner and
        ``suspected_flags()`` surfaces the rest.  So one write covers both
        outcomes: an acceptable value becomes a solve, a foreign-banner value
        becomes a reviewable candidate, and neither path can be confused with the
        other.
        """

        value = str(getattr(decision, "flag", "") or "").strip()
        if not value:
            return -1
        try:
            sequence = int(self._graph.flag_found(actor=actor, flag=value))
        except Exception:  # noqa: BLE001 - bookkeeping must not fail Finalize
            return -1
        self._sync_events()
        return sequence

    def fact_states(self) -> dict[int, dict[str, Any]]:
        """Return the official per-fact lifecycle state, keyed by fact sequence.

        Review arbitration is what turns a candidate fact into a *decision*
        (challenged / revalidated / rejected / merged / superseded).  Without
        this the platform can only ever add candidates, which is exactly why
        "待验证" grew without bound: nothing was allowed to conclude one.
        """

        reader = getattr(self._graph, "_fact_state_map", None)
        if not callable(reader):
            return {}
        try:
            return dict(reader() or {})
        except Exception:
            return {}

    def review_fact(
        self,
        *,
        actor: str,
        fact_seq: int,
        action: str,
        reason: str = "",
        verification_goal: str = "",
        to_fact_seq: int | None = None,
    ) -> dict[str, Any]:
        """Apply one official fact-review transition.

        ``fact_seq`` is the Blackboard sequence of the fact, not its position in
        any displayed list -- the official review tables key on the sequence.
        """

        reviewer = getattr(self._graph, "review_fact", None)
        if not callable(reviewer):
            return {"action": str(action), "fact_seq": int(fact_seq), "seq": -1}
        result = reviewer(
            actor=actor,
            fact_seq=int(fact_seq),
            action=str(action),
            reason=str(reason),
            verification_goal=str(verification_goal),
            to_fact_seq=to_fact_seq,
        )
        self._sync_events()
        return dict(result or {})

    def facts(self, *, verified_only: bool = False) -> list[Fact]:
        """Return active facts, keyed by their Blackboard sequence.

        ``fact_id`` is the *sequence*, matching ``MutekiGraph.facts`` and the
        official review tables.  It used to be a positional index, which made a
        fact unreviewable: the id a Reviewer held could not address the fact in
        the graph.  Facts that reached a terminal lifecycle state are dropped,
        mirroring the official ``snapshot``: a rejected/merged/superseded fact
        is a conclusion, not a pending question.
        """

        states = self.fact_states()
        facts: list[Fact] = []
        for event in self._graph.events_since(0, kinds=["fact_added"]):
            sequence = int(event.get("seq") or 0)
            if sequence <= 0:
                continue
            state = states.get(sequence, {})
            if str(state.get("state") or "") in {"rejected", "merged", "superseded"}:
                continue
            if state.get("retired"):
                continue
            verified = bool(event.get("verified"))
            effective = state.get("verified_effective")
            if effective is not None:
                verified = bool(effective)
            if verified_only and not verified:
                continue
            payload = event.get("payload") or {}
            artifact = str(event.get("artifact_id") or "")
            facts.append(
                Fact(
                    fact_id=sequence,
                    content=str(payload.get("fact") or ""),
                    source_worker_id=str(
                        payload.get("source_solver") or payload.get("source") or event.get("actor") or ""
                    ),
                    verified=verified,
                    created_at=str(event.get("ts") or ""),
                    evidence_refs=(artifact,) if artifact else (),
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

    def _matches_declared_format(self, flag: str) -> bool:
        """Does ``flag`` match the challenge's declared banner?

        Token-mode challenges (``flag_format == "token"``) declare no shape, so
        there is nothing to check and every value passes -- the same rule
        ``_harvest_flags`` applies.
        """

        value = str(flag or "").strip()
        if not value:
            return False
        if self.flag_pattern.strip().casefold() == _TOKEN_FLAG_FORMAT:
            return True
        return compile_flag_pattern(self.flag_pattern).fullmatch(value) is not None

#: Actor prefixes that mean "the host invented this spelling, no Worker read it".
#:
#: ``Coordinator._offer_recovered_flag`` writes a value the *box* printed under
#: its own banner, re-wrapped under the challenge's declared banner.  That is a
#: guess about the wrapper, and it is recorded deliberately (the value is worth
#: keeping) but it must never be mistaken for a Worker's finding.
#:
#: Measured 2026-10-25 on ``bestphp's revenge`` (run ``07d44a1b``): the recovered
#: ``CTF2{<uuid>}`` matched the declared ``CTF2\{[^}]+\}`` format, so ``flags()``
#: returned it, ``flag_found`` became True, ``report_json.flag_verified`` became
#: True, ``materialize_completion_gate_flag`` filed it ``VALID``, and the Run
    #: closed ``COMPLETED_SOLVED`` while the platform rejected all five spellings.
    #: The queue item stayed UNSOLVED, so the two ledgers disagreed, and the Run had
    #: burned 2.9 hours of the only environment slot.
    #:
    #: Format matching cannot separate these: the whole point of re-wrapping is to
    #: produce a value that *does* match.  Provenance is the only signal available,
    #: and ``actor`` is where it lives.
    _RECOVERED_ACTOR_PREFIX = "coordinator:recovery:"

    @staticmethod
    def _is_recovered_guess(actor: str) -> bool:
        """True when this flag_found row is a host-supplied wrapper, not a finding."""

        return str(actor or "").strip().casefold().startswith(
            UpstreamRuntimeGraph._RECOVERED_ACTOR_PREFIX
        )

    def _flag_found_events(self) -> list[tuple[int, str, str, Any]]:
        """Every harvested ``flag_found`` as ``(seq, flag, actor, timestamp)``."""

        rows: list[tuple[int, str, str, Any]] = []
        for event in self._graph.events():
            if event.get("kind") != "flag_found":
                continue
            payload = dict(event.get("payload") or {})
            rows.append(
                (
                    int(event.get("seq") or 0),
                    str(payload.get("flag") or ""),
                    str(event.get("actor") or "upstream"),
                    _event_timestamp(event),
                )
            )
        return rows

    def flags(self, *, verified_only: bool = False) -> list[Flag]:
        """Return flags that are verified *against this challenge's declaration*.

        The verdict of the side that produced the value is deliberately not
        trusted.  The official graph marks every ``flag_found`` it records as
        verified, and a Run that recorded the flag of the ORIGINAL public
        challenge -- a value that does not even match the declared banner --
        was reported ``COMPLETED_SOLVED`` and submitted, where the platform
        rejected it.  Only this host knows the declared format, so the format is
        re-applied here, the same way the harvest path already gates candidates.
        A value that fails is not discarded: it is surfaced by
        :meth:`suspected_flags` for a human to judge.
        """

        if verified_only is False:
            verified_only = True  # official graph only exposes gated flags here
        return [
            Flag(seq, flag, actor, True, timestamp)
            for seq, flag, actor, timestamp in self._flag_found_events()
            if self._matches_declared_format(flag) and not self._is_recovered_guess(actor)
        ]

    def suspected_flags(self) -> list[Flag]:
        """Flags the pipeline recorded but that are not a Worker's finding.

        Two distinct causes land here, and neither is a solve:

        * **wrong banner** -- the value came from somewhere other than the
          target (a public writeup, the original repository, the run's own
          notes), so its wrapper does not fit this challenge.
        * **host-supplied wrapper** -- :meth:`flags` excludes a
          ``coordinator:recovery:`` row even when its format matches, because the
          format matching is precisely what that path fabricated.  Without this
          second clause the excluded value would vanish from both lists and the
          evidence that it was tried would be lost.

        Handing them to a human is the point: only an operator may promote one.
        """

        return [
            Flag(seq, flag, actor, False, timestamp)
            for seq, flag, actor, timestamp in self._flag_found_events()
            if flag
            and (
                self._is_recovered_guess(actor)
                or not self._matches_declared_format(flag)
            )
        ]

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


# Punctuation a Worker commonly trails a flag with inside prose.  Braces are
# deliberately excluded: stripping ``}`` would truncate the candidate itself.
_FLAG_TRAILING_PUNCTUATION = ".,;:!?)]\"'`"
# Upstream sentinel for bare-token challenges (no braces at all).
_TOKEN_FLAG_FORMAT = "token"
_TOKEN_CANDIDATE_RE = re.compile(r"[A-Za-z0-9_-]{4,256}")


def _event_text(payload: Any) -> str:
    """Render an official event payload as the text the gate compares against."""

    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(payload)


def _looks_like_flag(
    candidate: str,
    matcher: re.Pattern[str] | None,
    *,
    token_mode: bool = False,
) -> bool:
    """Keep only a single, well-formed token that satisfies the challenge shape."""

    if not candidate or len(candidate) > 512:
        return False
    if any(char in candidate for char in "\r\n\"'\\"):
        return False
    if token_mode:
        return _TOKEN_CANDIDATE_RE.fullmatch(candidate) is not None
    if matcher is None or candidate.count("{") != 1 or candidate.count("}") != 1:
        return False
    try:
        return matcher.fullmatch(candidate) is not None
    except re.error:
        return False


__all__ = ["UpstreamRuntimeGraph", "create_runtime_graph", "runtime_graph_backend"]
