"""Per-Run process metrics: how a Run searched, not whether it won.

See ``docs/archive/2026-09-10/solver-capability-todo.md`` §11.3 / §11.4.  Both metrics here are
*negative* indicators — they can only falsify a knowledge-augmentation claim,
never confirm one — which is exactly why they are worth measuring next to a
success rate that is easy to inflate.

``repeat_attempts`` (CAP-14) needs no graph at all: a repeated attempt is the
same ``(tool, execution fingerprint, argument shape)`` seen more than once in
the ``tool_calls`` table.

``prior_misled`` (CAP-13, 口径 A) is computed from the graph event log only, so
it never touches the runtime's write path.  The production Worker already
writes the intent id into the dead-end description
(``app/solver/muteki/workers.py`` composes
``f"{kind} route={route} ... intent={job.intent_id}"``), so an intent that was
handed a PRIOR and then died can be attributed without adding a single event.

Both readers are read-only.  ``read_graph_events`` opens the SQLite file with
``mode=ro`` and never migrates, checkpoints, or writes.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from app.services.action_fingerprint import argument_shape, fingerprint_action

# The durable-telemetry event the Worker emits when it hands a prior to a turn.
# Written by ``muteki_runtime._worker_knowledge_block`` via ``graph.emit_event``.
KNOWLEDGE_PRIOR_EVENT = "runtime.worker.knowledge_prior"
_DEAD_END_KIND = "dead_end"
_INTENT_CONCLUDED_KIND = "intent_concluded"

# ``app/solver/muteki/workers.py`` writes ``intent=<id>`` into the dead-end text.
_INTENT_IN_DEAD_END = re.compile(r"\bintent=(\S+)")

# Failure labels actually produced by the two conclude sites:
#   * ``workers.py`` / ``cli_driver.py`` pass ``DeadEndKind`` (route_dead_end,
#     route_exhausted) or an engine status (FAILED, CANCELLED);
#   * ``coordinator._conclude_official_intent`` passes a ``WorkerResultCode``
#     (ROUTE_DEAD_END, ROUTE_EXHAUSTED, WORKER_FAILURE, EXTERNAL_BLOCKER).
# ``COMPLETED`` and ``PAUSED`` are deliberately absent: neither is a failure.
_FAILURE_RESULTS = frozenset(
    {
        "ROUTE_DEAD_END",
        "ROUTE_EXHAUSTED",
        "WORKER_FAILURE",
        "EXTERNAL_BLOCKER",
        "FAILED",
        "CANCELLED",
        "TIMEOUT",
        "BLOCKED",
        "STALLED",
    }
)

_FAILED_STATUSES = frozenset({"FAILED", "ERROR", "TIMEOUT", "REJECTED", "DENIED"})

# Both graph backends keep an event log, under different column names.
_EVENT_QUERIES = (
    # muteki/swarm/shared_graph.py (production default)
    "SELECT seq, kind, payload FROM events ORDER BY seq",
    # app/solver/muteki/graph.py (APP_MUTEKI_GRAPH_BACKEND=current)
    "SELECT sequence, event_type, payload_json FROM events ORDER BY sequence",
)

_GRAPH_DB_NAMES = ("upstream_shared_graph.db", "shared_graph.db")
_DETAIL_LIMIT = 8
_ITEM_LIMIT = 8


def graph_db_path(workspace_path: Any) -> Path | None:
    """Return the Run's graph database, or ``None`` when it has not been created."""

    text = str(workspace_path or "").strip()
    if not text:
        return None
    root = Path(text) / "muteki" / "graph"
    for name in _GRAPH_DB_NAMES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def read_graph_events(db_path: Any) -> list[dict[str, Any]]:
    """Read the graph event log read-only; return ``[]`` when unavailable.

    A missing, locked, or differently-schema'd graph must degrade to "no
    events", never raise: a process metric must not be able to fail a Run.
    """

    if db_path is None:
        return []
    path = Path(db_path)
    if not path.is_file():
        return []
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        rows = _select_event_rows(connection)
    except sqlite3.Error:
        return []
    finally:
        connection.close()

    events: list[dict[str, Any]] = []
    for sequence, kind, payload in rows:
        events.append(
            {
                "seq": int(sequence or 0),
                "kind": str(kind or ""),
                "payload": _parse_payload(payload),
            }
        )
    return events


def compute_prior_misled(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Count intents that were handed a PRIOR and then died.

    Guards that keep the number honest:

    * a prior with **no item ids** cannot mislead anyone toward anything, so it
      is excluded from the denominator rather than counted as a success;
    * a dead end only counts when it happened **after** the prior was handed
      over, so an intent that was already doomed before receiving knowledge is
      not retroactively blamed on it.  This is why the off arm reports ``None``
      instead of ``0``: with no prior events there is nothing to be misled by,
      and a bare ``0`` would read as a perfect score.
    """

    prior_items: dict[str, set[str]] = {}
    prior_seq: dict[str, int] = {}
    for event in events:
        if str(event.get("kind") or "") != KNOWLEDGE_PRIOR_EVENT:
            continue
        payload = event.get("payload") or {}
        if not isinstance(payload, Mapping):
            continue
        intent_id = str(payload.get("intent_id") or "").strip()
        if not intent_id:
            continue
        raw_items = payload.get("item_ids")
        items = {
            str(item).strip()
            for item in (raw_items if isinstance(raw_items, Iterable) else [])
            if str(item or "").strip()
        }
        prior_items.setdefault(intent_id, set()).update(items)
        prior_seq[intent_id] = max(prior_seq.get(intent_id, 0), int(event.get("seq") or 0))

    dead_ends: dict[str, int] = {}
    failures: dict[str, tuple[str, int]] = {}
    for event in events:
        kind = str(event.get("kind") or "")
        payload = event.get("payload") or {}
        if not isinstance(payload, Mapping):
            continue
        sequence = int(event.get("seq") or 0)
        if kind == _DEAD_END_KIND:
            match = _INTENT_IN_DEAD_END.search(str(payload.get("description") or ""))
            if match:
                dead_ends.setdefault(match.group(1), sequence)
        elif kind == _INTENT_CONCLUDED_KIND:
            intent_id = str(payload.get("intent_id") or "").strip()
            result = str(payload.get("result") or "").strip()
            if intent_id and result.upper() in _FAILURE_RESULTS:
                failures.setdefault(intent_id, (result, sequence))

    carrying = 0
    item_total = 0
    misled = 0
    detail: list[dict[str, Any]] = []
    for intent_id, items in sorted(prior_items.items()):
        if not items:
            continue
        carrying += 1
        item_total += len(items)
        handed_over_at = prior_seq.get(intent_id, 0)
        outcome = ""
        result = ""
        sequence: int | None = None
        if dead_ends.get(intent_id, 0) > handed_over_at:
            outcome, sequence = "dead_end", dead_ends[intent_id]
        elif failures.get(intent_id) and failures[intent_id][1] > handed_over_at:
            outcome, sequence = "failure", failures[intent_id][1]
            result = failures[intent_id][0]
        if not outcome:
            continue
        misled += 1
        detail.append(
            {
                "intent_id": intent_id,
                "item_ids": sorted(items)[:_ITEM_LIMIT],
                "outcome": outcome,
                "result": result,
                "prior_seq": handed_over_at,
                "outcome_seq": sequence,
            }
        )

    rate = (misled / carrying) if carrying else None
    return {
        "definition": (
            "intents handed a PRIOR carrying item ids whose dead end / failure "
            "landed after that PRIOR"
        ),
        "graph_events": len(events),
        "prior_carrying_intents": carrying,
        "prior_item_count": item_total,
        "misled_intents": misled,
        "misled_rate": round(rate, 6) if rate is not None else None,
        "detail": detail[:_DETAIL_LIMIT],
        "detail_truncated": max(len(detail) - _DETAIL_LIMIT, 0),
    }


def compute_repeat_attempts(calls: Sequence[Any]) -> dict[str, Any]:
    """Share of tool calls that re-ran something already seen, at two strengths.

    ``repeat_*`` is the strong signal: the same tool called with *byte-identical*
    arguments more than once, which can never be new information.

    ``same_shape_*`` is the weak signal: the same tool called with the same
    argument *keys* but possibly different values, i.e. the same idea retried
    with a tweaked payload.  It must never be read alone — a Worker that fetches
    20 distinct URLs shares one shape, so ordinary enumeration counts here too.
    Both are reported because collapsing them would either hide payload-tweak
    loops (strong only) or inflate the number with legitimate enumeration
    (shape only).

    Every ``ToolCall`` row for the Run counts; the per-group status distribution
    is reported so a reader can tell a genuine retry from a row that was created
    but never executed.  No status filter is applied, because inventing one would
    silently change the denominator between runs.
    """

    exact: dict[tuple[str, str], dict[str, Any]] = {}
    shapes: Counter[str] = Counter()
    total = 0
    for call in calls:
        tool = str(_field(call, "tool_name") or "")
        raw_arguments = _field(call, "arguments_json")
        arguments = dict(raw_arguments) if isinstance(raw_arguments, Mapping) else {}
        total += 1
        fingerprint = fingerprint_action(tool, arguments)
        shape = argument_shape(tool, arguments)
        entry = exact.setdefault(
            (tool, fingerprint),
            {
                "tool": tool,
                "shape": shape,
                "fingerprint": fingerprint,
                "count": 0,
                "statuses": Counter(),
            },
        )
        entry["count"] += 1
        entry["statuses"][str(_field(call, "status") or "UNKNOWN").upper()] += 1
        shapes[shape] += 1

    repeated = [entry for entry in exact.values() if entry["count"] >= 2]
    repeated.sort(key=lambda entry: (-entry["count"], entry["tool"], entry["fingerprint"]))
    repeat_calls = sum(int(entry["count"]) for entry in repeated)
    same_shape_calls = sum(count for count in shapes.values() if count >= 2)
    return {
        "definition": (
            "tool calls repeating an earlier call; repeat_* requires identical "
            "arguments, same_shape_* only the same argument keys"
        ),
        "total_calls": total,
        "distinct_calls": len(exact),
        "repeat_groups": len(repeated),
        "repeat_calls": repeat_calls,
        "repeat_ratio": (
            round(repeat_calls / total, 6) if total else None
        ),
        "same_shape_groups": sum(1 for count in shapes.values() if count >= 2),
        "same_shape_calls": same_shape_calls,
        "same_shape_ratio": (
            round(same_shape_calls / total, 6) if total else None
        ),
        "detail": [
            {
                "tool": entry["tool"],
                "shape": entry["shape"],
                "fingerprint": entry["fingerprint"],
                "count": int(entry["count"]),
                "statuses": dict(sorted(entry["statuses"].items())),
                "failed": any(status in _FAILED_STATUSES for status in entry["statuses"]),
            }
            for entry in repeated[:_DETAIL_LIMIT]
        ],
        "detail_truncated": max(len(repeated) - _DETAIL_LIMIT, 0),
    }


def compute_run_process_metrics(
    *,
    tool_calls: Sequence[Any] = (),
    graph_path: Any = None,
) -> dict[str, Any]:
    """Both process metrics for one Run, as JSON-serializable metadata."""

    return {
        "repeat_attempts": compute_repeat_attempts(tool_calls),
        "prior_misled": compute_prior_misled(read_graph_events(graph_path)),
    }


async def collect_run_process_metrics(
    session: Any,
    run_id: str,
    *,
    workspace_path: Any = None,
) -> dict[str, Any]:
    """Load a Run's tool calls, then compute both process metrics."""

    from sqlalchemy import select

    from app.models.run import SolveRun, ToolCall

    if workspace_path is None:
        run = await session.get(SolveRun, run_id)
        workspace_path = getattr(run, "workspace_path", None) if run is not None else None
    calls = list(
        (await session.scalars(select(ToolCall).where(ToolCall.run_id == run_id))).all()
    )
    return compute_run_process_metrics(
        tool_calls=calls,
        graph_path=graph_db_path(workspace_path),
    )


def _select_event_rows(connection: sqlite3.Connection) -> list[tuple[Any, Any, Any]]:
    last_error: sqlite3.Error | None = None
    for query in _EVENT_QUERIES:
        try:
            return list(connection.execute(query).fetchall())
        except sqlite3.Error as error:
            last_error = error
    if last_error is not None:
        raise last_error
    return []


def _parse_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _field(source: Any, name: str) -> Any:
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


async def _amain(run_ids: Sequence[str]) -> int:
    """Print the process metrics for explicit Run ids, so old runs can be back-filled."""

    from app.core.database import SessionLocal
    from app.models.run import SolveRun

    exit_code = 0
    async with SessionLocal() as session:
        for run_id in run_ids:
            run = await session.get(SolveRun, run_id)
            if run is None:
                print(json.dumps({"run_id": run_id, "error": "RUN_NOT_FOUND"}, ensure_ascii=False))
                exit_code = 3
                continue
            metrics = await collect_run_process_metrics(
                session,
                run_id,
                workspace_path=getattr(run, "workspace_path", None),
            )
            metrics["run_id"] = run_id
            metrics["challenge_id"] = str(getattr(run, "challenge_id", "") or "")
            print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m app.services.run_process_metrics <run_id>...``"""

    import sys

    arguments = list(sys.argv[1:] if argv is None else argv)
    run_ids = [argument for argument in arguments if not argument.startswith("-")]
    if not run_ids:
        print("usage: python -m app.services.run_process_metrics <run_id> [<run_id>...]")
        return 2
    return asyncio.run(_amain(run_ids))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "KNOWLEDGE_PRIOR_EVENT",
    "collect_run_process_metrics",
    "compute_prior_misled",
    "compute_repeat_attempts",
    "compute_run_process_metrics",
    "graph_db_path",
    "main",
    "read_graph_events",
]
