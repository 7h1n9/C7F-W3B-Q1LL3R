"""Focused tests for the per-Run process metrics (todo §11.3 / §11.4)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from app.benchmark.experiment import build_summary
from app.services.run_process_metrics import (
    KNOWLEDGE_PRIOR_EVENT,
    compute_prior_misled,
    compute_repeat_attempts,
    compute_run_process_metrics,
    graph_db_path,
    read_graph_events,
)

UPSTREAM_SCHEMA = (
    "CREATE TABLE events (seq INTEGER PRIMARY KEY, ts REAL, actor TEXT, kind TEXT, "
    "payload TEXT, artifact_id TEXT, verified INTEGER, confidence REAL)"
)
CURRENT_SCHEMA = (
    "CREATE TABLE events (sequence INTEGER PRIMARY KEY, timestamp TEXT, challenge_id TEXT, "
    "actor TEXT, event_type TEXT, payload_json TEXT, verified INTEGER, confidence REAL)"
)


def _workspace(tmp_path: Path, name: str) -> Path:
    root = tmp_path / "workspace" / "run-1" / "muteki" / "graph"
    root.mkdir(parents=True, exist_ok=True)
    return root / name


def _upstream_graph(tmp_path: Path, events: list[tuple[int, str, dict]]) -> Path:
    path = _workspace(tmp_path, "upstream_shared_graph.db")
    connection = sqlite3.connect(path)
    connection.execute(UPSTREAM_SCHEMA)
    for seq, kind, payload in events:
        connection.execute(
            "INSERT INTO events VALUES (?, 0.0, 'worker', ?, ?, NULL, 0, 1.0)",
            (seq, kind, json.dumps(payload)),
        )
    connection.commit()
    connection.close()
    return path


def _current_graph(tmp_path: Path, events: list[tuple[int, str, dict]]) -> Path:
    path = _workspace(tmp_path, "shared_graph.db")
    connection = sqlite3.connect(path)
    connection.execute(CURRENT_SCHEMA)
    for sequence, event_type, payload in events:
        connection.execute(
            "INSERT INTO events VALUES (?, '2026-10-06T00:00:00+00:00', 'run-1', 'worker', ?, ?, 0, 1.0)",
            (sequence, event_type, json.dumps(payload)),
        )
    connection.commit()
    connection.close()
    return path


def _call(tool: str, arguments: dict, status: str = "COMPLETED") -> SimpleNamespace:
    return SimpleNamespace(tool_name=tool, arguments_json=arguments, status=status)


def _prior(seq: int, intent_id: str, item_ids: list[str]) -> tuple[int, str, dict]:
    return (
        seq,
        KNOWLEDGE_PRIOR_EVENT,
        {"run_id": "run-1", "intent_id": intent_id, "item_ids": item_ids, "chars": 120},
    )


def _normalized(rows: list[tuple[int, str, dict]]) -> list[dict]:
    """The shape ``read_graph_events`` hands to the metric functions."""

    return [{"seq": seq, "kind": kind, "payload": payload} for seq, kind, payload in rows]


# --------------------------------------------------------------------------- reading


def test_reads_the_upstream_event_schema(tmp_path: Path) -> None:
    path = _upstream_graph(tmp_path, _all_events())
    events = read_graph_events(path)

    assert [event["kind"] for event in events] == [
        KNOWLEDGE_PRIOR_EVENT,
        "dead_end",
        "intent_concluded",
    ]
    assert events[0]["payload"]["intent_id"] == "intent-1"


def test_reads_the_current_event_schema(tmp_path: Path) -> None:
    path = _current_graph(tmp_path, _all_events())
    events = read_graph_events(path)

    assert len(events) == 3
    assert "intent=intent-1" in events[1]["payload"]["description"]


def test_a_missing_graph_is_an_empty_event_log(tmp_path: Path) -> None:
    assert graph_db_path(tmp_path / "nowhere") is None
    assert read_graph_events(None) == []
    assert read_graph_events(tmp_path / "absent.db") == []


def test_graph_db_path_returns_none_before_the_graph_exists(tmp_path: Path) -> None:
    (tmp_path / "workspace" / "run-1" / "muteki" / "graph").mkdir(parents=True)
    assert graph_db_path(tmp_path / "workspace" / "run-1") is None

    _upstream_graph(tmp_path, [])
    found = graph_db_path(tmp_path / "workspace" / "run-1")
    assert found is not None and found.name == "upstream_shared_graph.db"


# --------------------------------------------------------------------------- CAP-13


def _all_events() -> list[tuple[int, str, dict]]:
    return [
        _prior(1, "intent-1", ["sqli-union-based", "boolean-oracle"]),
        (
            2,
            "dead_end",
            {"description": "route_dead_end route=abc worker=w-1 intent=intent-1 reason=500"},
        ),
        (3, "intent_concluded", {"intent_id": "intent-1", "result": "route_dead_end"}),
    ]


def test_a_prior_followed_by_a_dead_end_counts_as_misled(tmp_path: Path) -> None:
    report = compute_prior_misled(read_graph_events(_upstream_graph(tmp_path, _all_events())))

    assert report["prior_carrying_intents"] == 1
    assert report["misled_intents"] == 1
    assert report["misled_rate"] == 1.0
    detail = report["detail"][0]
    assert detail["intent_id"] == "intent-1"
    assert detail["outcome"] == "dead_end"
    assert detail["item_ids"] == ["boolean-oracle", "sqli-union-based"]
    assert detail["outcome_seq"] > detail["prior_seq"]


def test_a_dead_end_before_the_prior_is_not_attributed_to_it() -> None:
    events = [
        (
            1,
            "dead_end",
            {"description": "route_exhausted route=abc worker=w-1 intent=intent-1"},
        ),
        _prior(2, "intent-1", ["sqli-union-based"]),
    ]
    report = compute_prior_misled(_normalized(events))

    assert report["prior_carrying_intents"] == 1
    assert report["misled_intents"] == 0
    assert report["misled_rate"] == 0.0


def test_a_prior_without_items_cannot_mislead_anyone() -> None:
    events = [
        _prior(1, "intent-1", []),
        (2, "dead_end", {"description": "route_dead_end intent=intent-1"}),
    ]
    report = compute_prior_misled(_normalized(events))

    # Excluded from the denominator: a prior carrying nothing cannot have
    # pointed anyone anywhere, so counting it would understate the rate.
    assert report["prior_carrying_intents"] == 0
    assert report["misled_intents"] == 0
    assert report["misled_rate"] is None


def test_the_off_arm_reports_none_rather_than_a_perfect_zero() -> None:
    events = [{"seq": 1, "kind": "dead_end", "payload": {"description": "no intent here"}}]
    report = compute_prior_misled(events)

    assert report["prior_carrying_intents"] == 0
    assert report["misled_intents"] == 0
    # A bare 0.0 here would read as "never misled", which is not what happened:
    # nothing was measurable.  ``None`` is the honest value.
    assert report["misled_rate"] is None


def test_only_failure_results_count_as_an_outcome() -> None:
    events = [
        _prior(1, "intent-ok", ["a"]),
        (2, "intent_concluded", {"intent_id": "intent-ok", "result": "COMPLETED"}),
        _prior(3, "intent-exhausted", ["b"]),
        (4, "intent_concluded", {"intent_id": "intent-exhausted", "result": "ROUTE_EXHAUSTED"}),
        _prior(5, "intent-worker-failure", ["c"]),
        (6, "intent_concluded", {"intent_id": "intent-worker-failure", "result": "WORKER_FAILURE"}),
    ]
    report = compute_prior_misled(_normalized(events))

    assert report["prior_carrying_intents"] == 3
    assert report["misled_intents"] == 2
    assert {entry["intent_id"] for entry in report["detail"]} == {
        "intent-exhausted",
        "intent-worker-failure",
    }
    assert report["misled_rate"] == round(2 / 3, 6)


def test_metrics_survive_a_json_round_trip(tmp_path: Path) -> None:
    report = compute_run_process_metrics(
        tool_calls=[_call("http_request", {"url": "http://x/1"})],
        graph_path=_upstream_graph(tmp_path, _all_events()),
    )
    restored = json.loads(json.dumps(report))

    assert restored["prior_misled"]["misled_intents"] == 1
    # One call is a denominator with no repetition, not an unknown ratio.
    assert restored["repeat_attempts"]["repeat_ratio"] == 0.0


# --------------------------------------------------------------------------- CAP-14


def test_identical_arguments_are_the_only_exact_repeat() -> None:
    report = compute_repeat_attempts(
        [
            _call("http_request", {"url": "http://x/1"}),
            _call("http_request", {"url": "http://x/1"}, status="FAILED"),
            _call("http_request", {"url": "http://x/2"}),
        ]
    )

    assert report["total_calls"] == 3
    assert report["distinct_calls"] == 2
    assert report["repeat_calls"] == 2
    assert report["repeat_ratio"] == round(2 / 3, 6)
    entry = report["detail"][0]
    assert entry["count"] == 2
    assert entry["failed"] is True
    assert entry["statuses"] == {"COMPLETED": 1, "FAILED": 1}


def test_a_tweaked_payload_counts_under_shape_but_not_under_exact() -> None:
    report = compute_repeat_attempts(
        [
            _call("http_request", {"url": "http://x/1", "body": "a"}),
            _call("http_request", {"body": "b", "url": "http://x/1"}),
            _call("file_read", {"path": "/etc/passwd"}),
        ]
    )

    # Same keys in a different order is the same shape: sorted keys.
    assert report["repeat_calls"] == 0
    assert report["repeat_ratio"] == 0.0
    assert report["same_shape_calls"] == 2
    assert report["same_shape_ratio"] == round(2 / 3, 6)


def test_enumeration_does_not_fake_an_exact_repeat() -> None:
    calls = [_call("http_request", {"url": f"http://x/{index}"}) for index in range(20)]
    report = compute_repeat_attempts(calls)

    assert report["repeat_calls"] == 0
    # This is exactly why the shape number is reported separately and never
    # alone: 20 distinct fetches share one shape.
    assert report["same_shape_calls"] == 20


def test_a_single_call_has_an_undefined_ratio() -> None:
    report = compute_repeat_attempts([_call("http_request", {"url": "http://x/1"})])

    assert report["repeat_calls"] == 0
    assert report["same_shape_calls"] == 0
    assert report["same_shape_ratio"] == 0.0
    assert compute_repeat_attempts([])["repeat_ratio"] is None
    assert compute_repeat_attempts([])["same_shape_ratio"] is None


# --------------------------------------------------------------------------- arm pooling


def _experiment_record(
    arm: str,
    *,
    repeat: int,
    success: bool,
    process_metrics: dict | None = None,
) -> dict:
    record: dict = {
        "case_id": "case-sqli",
        "arm": arm,
        "repeat": repeat,
        "status": "COMPLETED",
        "success": success,
        "error": None,
        "evaluation_report": {"final_result": "SUCCESS" if success else "INCOMPLETE"},
    }
    if process_metrics is not None:
        record["metadata"] = {"process_metrics": process_metrics}
    return record


def test_an_arm_without_metrics_reports_nulls_rather_than_zeros() -> None:
    metrics = compute_run_process_metrics(
        tool_calls=[
            _call("http_request", {"url": "http://x/1"}),
            _call("http_request", {"url": "http://x/1"}),
        ],
        graph_path=None,
    )
    summary = build_summary(
        [
            _experiment_record("off", repeat=1, success=False),
            _experiment_record("on", repeat=1, success=True, process_metrics=metrics),
        ],
        arms=("off", "on"),
    )

    off = summary["by_arm"]["off"]["process_metrics"]
    on = summary["by_arm"]["on"]["process_metrics"]
    assert off["runs_measured"] == 0
    assert off["repeat_attempts"]["pooled_repeat_ratio"] is None
    assert off["prior_misled"]["pooled_misled_rate"] is None

    assert on["runs_measured"] == 1
    assert on["repeat_attempts"]["repeat_calls"] == 2
    assert on["repeat_attempts"]["pooled_repeat_ratio"] == 1.0
    # A Run with no graph produced no prior events: the rate stays unknown.
    assert on["prior_misled"]["runs_with_graph"] == 0
    assert on["prior_misled"]["pooled_misled_rate"] is None


def test_an_arm_with_priors_pools_the_misled_rate(tmp_path: Path) -> None:
    metrics = compute_run_process_metrics(
        tool_calls=[],
        graph_path=_upstream_graph(tmp_path, _all_events()),
    )
    summary = build_summary(
        [_experiment_record("on", repeat=1, success=False, process_metrics=metrics)],
        arms=("on",),
    )

    block = summary["by_arm"]["on"]["process_metrics"]["prior_misled"]
    assert block["runs_with_graph"] == 1
    assert block["runs_that_received_a_prior"] == 1
    assert block["prior_carrying_intents"] == 1
    assert block["misled_intents"] == 1
    assert block["pooled_misled_rate"] == 1.0
