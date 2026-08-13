"""Common Worker outcome and route dead-end semantics.

The upstream Muteki Worker reports a route dead-end only when the Worker
explicitly rules out its assigned direction.  A provider timeout, an
authentication failure, a malformed response, or an Evidence bridge failure
is an execution problem, not proof that the target route is impossible.

This module is deliberately independent of any model provider.  Codex,
OpenAI-compatible Workers, compatibility callbacks, and the native Sandbox
all use the same small vocabulary at the Coordinator boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping


class WorkerResultCode(StrEnum):
    """Meaning of one bounded Worker assignment."""

    COMPLETED = "COMPLETED"
    ROUTE_DEAD_END = "ROUTE_DEAD_END"
    ROUTE_EXHAUSTED = "ROUTE_EXHAUSTED"
    WORKER_FAILURE = "WORKER_FAILURE"
    EXTERNAL_BLOCKER = "EXTERNAL_BLOCKER"


class DeadEndKind(StrEnum):
    """Kinds that may be persisted as a target-route dead-end."""

    ROUTE_DEAD_END = "route_dead_end"
    ROUTE_EXHAUSTED = "route_exhausted"


@dataclass(frozen=True, slots=True)
class DeadEndSignal:
    """A safe, bounded signal produced by a Worker for one route."""

    kind: DeadEndKind
    reason: str
    route_hash: str = ""
    evidence_refs: tuple[str, ...] = ()
    # Native Muteki's CliSolver writes the dead-end to its SharedGraph before
    # the adapter returns.  The Coordinator still consumes the signal, but
    # must not append a second record to the facade graph.
    already_persisted: bool = False


_DEAD_END_LINE = re.compile(
    r"^\s*(?:DEADEND|DEAD_END|ROUTE_DEAD_END)\s*[:=]\s*(.+?)\s*$",
    re.IGNORECASE,
)
_SECRET_RE = re.compile(
    r"""(?:
        (?:api[_-]?key|token|secret|password|cookie)\s*[:=]\s*[^\s,;]+|
        (?:bearer\s+)[^\s,;]+|
        -----BEGIN [A-Z ]*PRIVATE KEY-----
    )""",
    re.IGNORECASE | re.VERBOSE,
)
_SAFE_ROUTE_RE = re.compile(r"[^A-Za-z0-9_.:/@-]+")


def extract_dead_end_reasons(text: object) -> tuple[str, ...]:
    """Extract explicit Muteki ``DEADEND=`` markers from Worker text.

    Only marker-shaped lines are accepted.  Ordinary prose mentioning a dead
    end is not enough to mutate the shared graph, matching the upstream
    parser's explicit-marker contract.
    """

    values: list[str] = []
    seen: set[str] = set()
    for line in str(text or "").splitlines():
        match = _DEAD_END_LINE.match(line)
        if not match:
            continue
        reason = sanitize_dead_end_reason(match.group(1))
        if reason and reason.casefold() not in seen:
            seen.add(reason.casefold())
            values.append(reason)
    return tuple(values)


def sanitize_dead_end_reason(value: object, *, limit: int = 240) -> str:
    """Return a safe reason suitable for the append-only Graph event."""

    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = _SECRET_RE.sub("[redacted]", text)
    return text[: max(1, int(limit))].strip()


def route_key(payload: Mapping[str, Any] | None, *, intent_id: str = "", goal: str = "") -> str:
    """Resolve a stable route identity without storing request data."""

    values = payload or {}
    candidate = (
        values.get("route_hash")
        or values.get("route")
        or intent_id
        or values.get("tool_name")
        or goal
        or "unidentified-route"
    )
    return _SAFE_ROUTE_RE.sub("_", str(candidate).strip())[:160] or "unidentified-route"


def stable_route_hash(
    payload: Mapping[str, Any] | None = None,
    *,
    intent_id: str = "",
    goal: str = "",
) -> str:
    """Return the canonical route identity used by Coordinator reservations.

    A model-supplied route keeps its semantic label.  When a provider omits
    one, derive a bounded digest from the goal and action metadata.  Engine
    identity and attempt identity are intentionally excluded so two engines
    cannot turn the same target route into two apparently different routes.
    """

    values = dict(payload or {})
    supplied = values.get("route_hash") or values.get("route")
    if supplied:
        return route_key({"route_hash": str(supplied)})
    identity = {
        "goal": str(goal or "").strip().casefold(),
        "tool_name": str(values.get("tool_name") or values.get("tool") or "").strip().casefold(),
        "classification": str(values.get("classification") or "").strip().casefold(),
        "lane_key": str(values.get("lane_key") or "").strip().casefold(),
        "arguments": values.get("arguments") if isinstance(values.get("arguments"), Mapping) else {},
    }
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return f"route:{hashlib.sha1(encoded.encode('utf-8', 'ignore')).hexdigest()[:16]}"


def stable_branch_id(route_hash: str, branch_id: str = "") -> str:
    """Return a stable branch identity for a route without engine coupling."""

    value = str(branch_id or "").strip()
    return value[:160] if value else f"branch:{stable_route_hash({'route_hash': route_hash})}"


def signal_from_worker_text(
    text: object,
    *,
    payload: Mapping[str, Any] | None = None,
    intent_id: str = "",
    goal: str = "",
    evidence_refs: tuple[str, ...] = (),
    already_persisted: bool = False,
) -> DeadEndSignal | None:
    """Turn an explicit Worker marker into the common route signal."""

    reasons = extract_dead_end_reasons(text)
    if not reasons:
        return None
    return DeadEndSignal(
        kind=DeadEndKind.ROUTE_DEAD_END,
        reason=reasons[0],
        route_hash=route_key(payload, intent_id=intent_id, goal=goal),
        evidence_refs=tuple(str(item) for item in evidence_refs if str(item)),
        already_persisted=already_persisted,
    )


def failure_code(status: object, metadata: Mapping[str, Any] | None = None) -> WorkerResultCode:
    """Classify execution failure without declaring a target dead-end."""

    normalized = str(status or "").strip().upper()
    reason = str((metadata or {}).get("reason") or "").upper()
    combined = f"{normalized} {reason}"
    if any(value in combined for value in ("CREDENTIAL", "AUTH", "API_KEY", "QUOTA", "RATE_LIMIT", "PROVIDER", "ENDPOINT")):
        return WorkerResultCode.EXTERNAL_BLOCKER
    return WorkerResultCode.WORKER_FAILURE


__all__ = [
    "DeadEndKind",
    "DeadEndSignal",
    "WorkerResultCode",
    "extract_dead_end_reasons",
    "failure_code",
    "route_key",
    "stable_branch_id",
    "stable_route_hash",
    "sanitize_dead_end_reason",
    "signal_from_worker_text",
]
