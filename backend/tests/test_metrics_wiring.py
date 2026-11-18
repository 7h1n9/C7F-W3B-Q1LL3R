"""The metrics guard is only real if an analysis script calls it.

``app.services.metrics_guard`` shipped with no production caller: the rules
existed, nothing enforced them, and three wrong-number decisions happened
anyway.  These tests pin the wiring -- that ``record_analysis`` writes a
recomputable row, that an empty source is a bug rather than a warning, that a
full disk cannot cost you the analysis, and that the two scripts call it without
changing what they return.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import _metrics_boot  # noqa: E402
import analyze_truncation_points  # noqa: E402
import export_training_transitions  # noqa: E402
from _metrics_boot import record_analysis  # noqa: E402

# ---------------------------------------------------------------------------
# record_analysis itself
# ---------------------------------------------------------------------------


def test_writes_one_recomputable_row(tmp_path):
    target = tmp_path / "snapshots.jsonl"

    record_analysis(
        "analyze_truncation_points",
        "transitions jsonl: data/training/x.jsonl | 行数限制: 无",
        {"unsolved": {"n": 3}},
        filters={"window": 10},
        data_window="全量",
        path=target,
    )

    rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["query_id"].startswith("analyze_truncation_points-")
    assert row["sql_or_source"] == "transitions jsonl: data/training/x.jsonl | 行数限制: 无"
    assert row["filters"] == {"window": 10}
    assert row["data_window"] == "全量"
    assert row["result"] == {"unsolved": {"n": 3}}
    # code_mtime is what makes the row attributable to a code version.
    assert row["code_mtime"].endswith("Z")
    assert row["computed_at"].endswith("Z")


def test_default_log_is_absolute_and_repo_anchored():
    """A CWD-relative default would hide rows in backend/data/logs during tests."""

    assert _metrics_boot.SNAPSHOT_LOG.is_absolute()
    assert _metrics_boot.SNAPSHOT_LOG == ROOT / "data" / "logs" / "metrics-snapshots.jsonl"


@pytest.mark.parametrize("source", ["", "   ", None])
def test_empty_source_raises_instead_of_recording_blindly(tmp_path, source):
    target = tmp_path / "snapshots.jsonl"

    with pytest.raises(ValueError, match="sql_or_source"):
        record_analysis("analyze_truncation_points", source, {"n": 1}, path=target)

    assert not target.exists()


def test_unwritable_target_warns_and_does_not_raise(tmp_path, capsys):
    """A directory where the log file should be is the cheapest full-disk stand-in."""

    target = tmp_path / "a_directory"
    target.mkdir()

    record_analysis(
        "analyze_truncation_points",
        "transitions jsonl: x | 行数限制: 无",
        {"n": 1},
        path=target,
    )

    err = capsys.readouterr().err
    assert "快照未写入" in err
    assert "本次出数不受影响" in err


# ---------------------------------------------------------------------------
# The scripts call it, and still return what they returned before
# ---------------------------------------------------------------------------


def _write_synthetic_transitions(path: Path) -> None:
    """Two runs: one stuck (fact0/recon), one solved (exploit tail)."""

    rows = []
    for sequence, command in enumerate(["ls -la", "cat /etc/passwd", "ls -la"], start=1):
        rows.append(
            {
                "run_id": "run-stuck-0001",
                "sequence": sequence,
                "labels": {"final_status": "COMPLETED_FAILED"},
                "action": {"command": command},
                "observation": {"delta_facts": 0},
            }
        )
    for sequence, command in enumerate(["curl -X POST /x", "sqlmap -u y"], start=1):
        rows.append(
            {
                "run_id": "run-solved-0002",
                "sequence": sequence,
                "labels": {"final_status": "COMPLETED_SOLVED"},
                "action": {"command": command},
                "observation": {"delta_facts": 1},
            }
        )
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_truncation_analysis_records_and_still_returns_zero(tmp_path, monkeypatch):
    source = tmp_path / "transitions-synthetic.jsonl"
    _write_synthetic_transitions(source)
    target = tmp_path / "snapshots.jsonl"

    monkeypatch.setattr(
        sys, "argv", ["analyze_truncation_points.py", "--src", str(source), "--window", "2"]
    )
    monkeypatch.setattr(_metrics_boot, "SNAPSHOT_LOG", target)

    assert analyze_truncation_points.main() == 0

    rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["sql_or_source"].startswith(f"transitions jsonl: {source.as_posix()}")
    assert "无" in row["sql_or_source"]
    assert row["result"]["runs"] == 2
    assert row["result"]["transitions"] == 5
    assert row["result"]["unsolved"]["n"] == 1
    assert row["result"]["unsolved"]["median_commands"] == 3
    assert row["result"]["solved"]["n"] == 1
    # The quadrant the script prints is also the number the row stores.
    assert row["result"]["stuck_fact0_recon"] == 1
    assert row["result"]["quadrant"]["fact0 / recon"] == 1


def test_truncation_analysis_exit_code_ignores_a_failed_snapshot(tmp_path, monkeypatch, capsys):
    source = tmp_path / "transitions-synthetic.jsonl"
    _write_synthetic_transitions(source)
    blocked = tmp_path / "blocked"
    blocked.mkdir()

    monkeypatch.setattr(sys, "argv", ["analyze_truncation_points.py", "--src", str(source)])
    monkeypatch.setattr(_metrics_boot, "SNAPSHOT_LOG", blocked)

    assert analyze_truncation_points.main() == 0
    assert "快照未写入" in capsys.readouterr().err


def test_export_records_its_manifest_numbers(tmp_path, monkeypatch):
    assert export_training_transitions.record_analysis is _metrics_boot.record_analysis

    target = tmp_path / "snapshots.jsonl"
    run = {
        "id": "run-1",
        "challenge_id": "ch-1",
        "status": "COMPLETED_SOLVED",
        "last_error_code": "",
        "last_error_message": "",
        "created_at": "2026-11-01T00:00:00",
        "fresh_repro": 1,
        "challenge_name": "demo",
    }
    monkeypatch.setattr(export_training_transitions, "fetch_runs", lambda args: [run])
    monkeypatch.setattr(export_training_transitions, "fetch_accepted_run_ids", lambda: {"run-1"})
    monkeypatch.setattr(export_training_transitions, "fetch_known_secrets", lambda: ())
    monkeypatch.setattr(
        export_training_transitions, "fetch_events", lambda run_ids: {rid: [] for rid in run_ids}
    )

    monkeypatch.setattr(
        sys,
        "argv",
        ["export_training_transitions.py", "--dry-run", "--output-dir", str(tmp_path / "out")],
    )
    monkeypatch.setattr(_metrics_boot, "SNAPSHOT_LOG", target)

    assert export_training_transitions.main() == 0

    rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["result"]["runs_total"] == 1
    assert row["result"]["runs_included"] + row["result"]["runs_excluded"] == 1
    assert row["filters"]["dry_run"] is True
    assert row["filters"]["seed"] == "c7f-v1"
    assert "run_events" in row["sql_or_source"]
    assert not list((tmp_path / "out").glob("transitions-*.jsonl"))
