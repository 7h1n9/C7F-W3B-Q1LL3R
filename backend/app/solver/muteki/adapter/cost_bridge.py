"""Durable Token/USD accounting for the native Muteki Worker boundary.

The canonical Muteki cost controller is intentionally process-local.  The
application already has a durable, ordered ``RunEvent`` stream, so native
Worker usage is projected into cumulative ``cost.update`` events here.  This
keeps the bridge additive: it does not add a database table or make the
Worker's text output authoritative.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.models.run import RunEvent
from app.services.events import event_service

_COST_EVENT_TYPES = frozenset({"cost.update", "muteki.cost.update"})
_DEFAULT_INPUT_PER_M = 1.0
_DEFAULT_OUTPUT_PER_M = 3.0
_run_locks: dict[str, asyncio.Lock] = {}


@dataclass(frozen=True, slots=True)
class MutekiUsage:
    """One native Worker usage delta."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, other: "MutekiUsage") -> "MutekiUsage":
        return MutekiUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
            calls=self.calls + other.calls,
        )


def usage_breakdown_from_events(events: Iterable[Any]) -> list[dict[str, Any]]:
    """Return durable usage grouped by model/role without exposing prompts.

    Muteki emits cumulative ``cost.update`` snapshots for compatibility and
    the application bridge emits delta fields for replay-safe accounting. The
    grouping mirrors :func:`usage_from_events`: delta events are summed, while
    older cumulative-only events contribute their latest snapshot per model.
    """

    deltas: dict[tuple[str, str, str], MutekiUsage] = {}
    snapshots: dict[tuple[str, str, str], MutekiUsage] = {}
    delta_seen: set[tuple[str, str, str]] = set()
    labels: dict[tuple[str, str, str], dict[str, str]] = {}
    for event in events:
        if str(getattr(event, "event_type", "")) not in _COST_EVENT_TYPES:
            continue
        payload = _unwrap_payload(getattr(event, "payload_json", {}))
        if not isinstance(payload, Mapping):
            continue
        model = str(payload.get("model") or payload.get("engine") or "unknown").strip()[:120] or "unknown"
        role = str(payload.get("role") or ("worker" if payload.get("engine") else "coordinator_reason")).strip()[:80] or "unknown"
        source = str(payload.get("source") or "muteki").strip()[:120] or "muteki"
        key = (model, role, source)
        labels[key] = {"model": model, "role": role, "source": source}
        if any(key_name in payload for key_name in ("delta_usd", "delta_tokens", "delta_input_tokens", "delta_output_tokens")):
            delta_seen.add(key)
            deltas[key] = deltas.get(key, MutekiUsage()).add(
                MutekiUsage(
                    input_tokens=_nonnegative_int(payload.get("delta_input_tokens")),
                    output_tokens=_nonnegative_int(payload.get("delta_output_tokens")),
                    cost_usd=_nonnegative_float(payload.get("delta_usd")),
                    calls=max(0, _nonnegative_int(payload.get("delta_calls"))),
                )
            )
        else:
            snapshots[key] = MutekiUsage(
                input_tokens=_nonnegative_int(payload.get("input_tokens")),
                output_tokens=_nonnegative_int(payload.get("output_tokens")),
                cost_usd=_nonnegative_float(payload.get("cost_usd", payload.get("usd"))),
                calls=max(0, _nonnegative_int(payload.get("calls"))),
            )

    rows: list[dict[str, Any]] = []
    for key in labels:
        usage = deltas.get(key, MutekiUsage())
        if key not in delta_seen:
            usage = snapshots.get(key, usage)
        elif key in snapshots:
            usage = usage.add(snapshots[key])
        row = {
            **labels[key],
            "calls": usage.calls,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "total_tokens": usage.tokens,
            "cost_usd": round(usage.cost_usd, 10),
        }
        if usage.calls or usage.tokens or usage.cost_usd:
            rows.append(row)
    return sorted(rows, key=lambda item: (-int(item["total_tokens"]), str(item["model"])))


def usage_from_result(result: Any) -> MutekiUsage:
    """Extract only numeric usage metadata from an OfficialWorker result.

    ``result.output`` is deliberately never inspected.  If an older driver
    does not provide ``cost_usd``, the known Muteki model price table is used
    as a bounded compatibility fallback.
    """

    metadata = getattr(result, "metadata", {})
    if not isinstance(metadata, Mapping):
        metadata = {}
    # A cancelled native Worker may flush its CostController snapshot from the
    # adapter recovery path before the asyncio task returns.  Do not count the
    # same snapshot again when that late result is observed.
    if metadata.get("_usage_already_recorded"):
        return MutekiUsage()
    input_tokens = _nonnegative_int(metadata.get("input_tokens"))
    output_tokens = _nonnegative_int(metadata.get("output_tokens"))
    cost_value = metadata.get("cost_usd", metadata.get("usd"))
    cost_usd = _nonnegative_float(cost_value)
    if cost_value is None and (input_tokens or output_tokens):
        cost_usd = _price_tokens(
            str(getattr(result, "engine", "") or ""), input_tokens, output_tokens
        )
    if not (input_tokens or output_tokens or cost_usd):
        return MutekiUsage()
    return MutekiUsage(input_tokens, output_tokens, cost_usd, calls=1)


def usage_from_events(events: Iterable[Any]) -> MutekiUsage:
    """Rebuild native usage from durable cost events.

    New bridge events carry ``delta_*`` fields plus cumulative fields.  The
    delta fields make restart/replay aggregation unambiguous.  Older
    cumulative-only events remain readable by taking their latest snapshot.
    """

    delta_total = MutekiUsage()
    delta_seen = False
    latest_snapshot = MutekiUsage()
    for event in events:
        event_type = str(getattr(event, "event_type", ""))
        if event_type not in _COST_EVENT_TYPES:
            continue
        payload = _unwrap_payload(getattr(event, "payload_json", {}))
        if not isinstance(payload, Mapping):
            continue
        if any(key in payload for key in ("delta_usd", "delta_tokens", "delta_input_tokens", "delta_output_tokens")):
            delta_seen = True
            delta_total = delta_total.add(
                MutekiUsage(
                    input_tokens=_nonnegative_int(payload.get("delta_input_tokens")),
                    output_tokens=_nonnegative_int(payload.get("delta_output_tokens")),
                    cost_usd=_nonnegative_float(payload.get("delta_usd")),
                    calls=max(0, _nonnegative_int(payload.get("delta_calls"))),
                )
            )
            continue
        latest_snapshot = MutekiUsage(
            input_tokens=_nonnegative_int(payload.get("input_tokens")),
            output_tokens=_nonnegative_int(payload.get("output_tokens")),
            cost_usd=_nonnegative_float(payload.get("cost_usd", payload.get("usd"))),
            calls=max(0, _nonnegative_int(payload.get("calls"))),
        )
    return delta_total.add(latest_snapshot) if delta_seen else latest_snapshot


async def record_official_worker_usage(
    session_factory: Any,
    *,
    run_id: str,
    worker_id: str,
    intent_id: str | None,
    result: Any,
) -> MutekiUsage | None:
    """Persist one native Worker usage delta as a cumulative RunEvent.

    The per-run lock prevents concurrent Workers in one application process
    from calculating the same cumulative total.  If usage is unavailable, no
    placeholder event is emitted and the UI correctly keeps showing ``—``.
    """

    delta = usage_from_result(result)
    if not delta.calls:
        return None
    run_key = str(run_id)
    lock = _run_locks.setdefault(run_key, asyncio.Lock())
    async with lock:
        async with session_factory() as session:
            history = await event_service.history(session, run_key)
            previous = usage_from_events(history)
            cumulative = previous.add(delta)
            payload = {
                "scope": "run",
                "source": "muteki.official_worker",
                "run_id": run_key,
                "worker_id": str(worker_id)[:120],
                "intent_id": str(intent_id or "")[:120],
                "engine": str(getattr(result, "engine", "") or "")[:80],
                "model": str(
                    _metadata_value(getattr(result, "metadata", None), "model")
                    or getattr(result, "engine", "")
                    or ""
                )[:120],
                "role": str(
                    _metadata_value(getattr(result, "metadata", None), "role")
                    or "worker"
                )[:80],
                "delta_usd": round(delta.cost_usd, 10),
                "delta_tokens": delta.tokens,
                "delta_input_tokens": delta.input_tokens,
                "delta_output_tokens": delta.output_tokens,
                "delta_calls": delta.calls,
                "usd": round(cumulative.cost_usd, 10),
                "cost_usd": round(cumulative.cost_usd, 10),
                "tokens": cumulative.tokens,
                "total_tokens": cumulative.tokens,
                "input_tokens": cumulative.input_tokens,
                "output_tokens": cumulative.output_tokens,
                "calls": cumulative.calls,
            }
            await event_service.append(session, run_key, "cost.update", payload)
            return cumulative


async def record_muteki_reason_usage(
    session_factory: Any,
    *,
    run_id: str,
    model_config_id: str,
    model_name: str,
    trace: Mapping[str, Any],
    source: str = "muteki.coordinator_reason",
    role: str = "coordinator_reason",
) -> MutekiUsage | None:
    """Persist Coordinator Reason usage without storing model response text."""

    class _ReasonResult:
        engine = model_name
        metadata = trace

    delta = usage_from_result(_ReasonResult())
    if not delta.calls:
        return None
    run_key = str(run_id)
    lock = _run_locks.setdefault(run_key, asyncio.Lock())
    async with lock:
        async with session_factory() as session:
            history = await event_service.history(session, run_key)
            cumulative = usage_from_events(history).add(delta)
            await event_service.append(
                session,
                run_key,
                "cost.update",
                {
                    "scope": "run",
                    "source": source,
                    "run_id": run_key,
                    "model_config_id": str(model_config_id)[:120],
                    "model": str(model_name)[:120],
                    "role": role,
                    "delta_usd": round(delta.cost_usd, 10),
                    "delta_tokens": delta.tokens,
                    "delta_input_tokens": delta.input_tokens,
                    "delta_output_tokens": delta.output_tokens,
                    "delta_calls": delta.calls,
                    "usd": round(cumulative.cost_usd, 10),
                    "cost_usd": round(cumulative.cost_usd, 10),
                    "tokens": cumulative.tokens,
                    "total_tokens": cumulative.tokens,
                    "input_tokens": cumulative.input_tokens,
                    "output_tokens": cumulative.output_tokens,
                    "calls": cumulative.calls,
                },
            )
            return cumulative


def _unwrap_payload(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    nested = payload.get("payload")
    return nested if isinstance(nested, Mapping) else payload


def _metadata_value(metadata: Any, key: str) -> Any:
    """Read one non-sensitive result metadata field without inspecting output."""

    return metadata.get(key) if isinstance(metadata, Mapping) else None


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _nonnegative_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return max(0.0, number)


def _price_tokens(engine: str, input_tokens: int, output_tokens: int) -> float:
    try:
        from muteki.core.cost import PRICES

        price = PRICES.get(engine.casefold(), PRICES.get("codex"))
        if price is not None:
            return float(price.cost(input_tokens, output_tokens))
    except Exception:
        pass
    return (
        input_tokens / 1_000_000 * _DEFAULT_INPUT_PER_M
        + output_tokens / 1_000_000 * _DEFAULT_OUTPUT_PER_M
    )


async def load_muteki_usage(session: Any, run_id: str) -> MutekiUsage:
    """Load the durable native usage summary for report generation."""

    events = list(
        (
            await session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == str(run_id), RunEvent.event_type.in_(_COST_EVENT_TYPES))
                .order_by(RunEvent.event_id, RunEvent.sequence)
            )
        ).all()
    )
    return usage_from_events(events)


async def load_muteki_usage_breakdown(session: Any, run_id: str) -> list[dict[str, Any]]:
    """Load the safe per-model usage rows used by the Workspace inspector."""

    events = list(
        (
            await session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == str(run_id), RunEvent.event_type.in_(_COST_EVENT_TYPES))
                .order_by(RunEvent.event_id, RunEvent.sequence)
            )
        ).all()
    )
    return usage_breakdown_from_events(events)


__all__ = [
    "MutekiUsage",
    "load_muteki_usage",
    "load_muteki_usage_breakdown",
    "record_official_worker_usage",
    "usage_breakdown_from_events",
    "record_muteki_reason_usage",
    "usage_from_events",
    "usage_from_result",
]
