"""The remaining three guardrails must have a caller, not just a docstring.

``format_comparison``, ``check_preregistration`` and ``coverage_report`` shipped
with zero callers: the rules existed, so a review could say "we have a guardrail"
while every reported number still bypassed it.  These tests pin the wiring:

* ``analyze_truncation_points`` feeds its unsolved-vs-solved split through
  ``format_comparison``, and its snapshot stores the same numerators/denominators.
* ``prepare_fa_causal`` refuses to emit a grouping when the preregistration is
  incomplete -- including under ``--dry-run``, which used to be a side door.
* ``event_coverage`` reports ZERO for a silent feature and refuses to report 0
  when the database is unreachable.

They also pin the *limits*: the truncation split is grouped by ``final_status``,
i.e. by the outcome, so a clean guardrail output is still not a causal claim.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import analyze_truncation_points  # noqa: E402
import _metrics_boot  # noqa: E402
import event_coverage  # noqa: E402
import prepare_fa_causal  # noqa: E402

# ---------------------------------------------------------------------------
# 1. format_comparison wired into analyze_truncation_points
# ---------------------------------------------------------------------------


def _write_transitions(path: Path, *, unsolved_runs: int, solved_runs: int) -> None:
    rows = []
    for index in range(unsolved_runs):
        for sequence, command in enumerate(["ls -la", "cat /etc/passwd", "ls -la"], start=1):
            rows.append(
                {
                    "run_id": f"run-unsolved-{index}",
                    "sequence": sequence,
                    "labels": {"final_status": "COMPLETED_UNSOLVED"},
                    "action": {"command": command},
                    "observation": {"delta_facts": 0},
                }
            )
    for index in range(solved_runs):
        for sequence, command in enumerate(["curl -X POST /x", "sqlmap -u y"], start=1):
            rows.append(
                {
                    "run_id": f"run-solved-{index}",
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


def test_truncation_split_goes_through_the_guard(tmp_path, monkeypatch, capsys):
    source = tmp_path / "transitions.jsonl"
    _write_transitions(source, unsolved_runs=4, solved_runs=3)
    monkeypatch.setattr(sys, "argv", ["analyze_truncation_points.py", "--src", str(source)])
    monkeypatch.setattr(_metrics_boot, "SNAPSHOT_LOG", tmp_path / "snapshots.jsonl")

    assert analyze_truncation_points.main() == 0
    out = capsys.readouterr().out

    assert "[组间对比]" in out
    # Both denominators are exposed verbatim, never merged or resampled.
    assert "未解=4，已解=3" in out
    assert "两组样本量(分母): 4 vs 3" in out
    assert "causal_claim_allowed =" in out
    # The semantic warning is outside the guardrail's reach and must be printed.
    assert "按结局分组" in out
    assert "不能解释为" in out


def test_truncation_snapshot_stores_the_comparison_number(tmp_path, monkeypatch):
    source = tmp_path / "transitions.jsonl"
    _write_transitions(source, unsolved_runs=3, solved_runs=3)
    log = tmp_path / "snapshots.jsonl"
    monkeypatch.setattr(sys, "argv", ["analyze_truncation_points.py", "--src", str(source)])
    monkeypatch.setattr(_metrics_boot, "SNAPSHOT_LOG", log)

    assert analyze_truncation_points.main() == 0

    row = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    comparison = row["result"]["comparison"]
    assert comparison["denominator_a"] == 3
    assert comparison["denominator_b"] == 3
    assert comparison["numerator_a"] == 0  # unsolved runs never grow facts here
    assert comparison["numerator_b"] == 3
    assert isinstance(comparison["causal_claim_allowed"], bool)
    assert "final_status" in comparison["grouping_variable"]


def test_guard_warning_is_visible_when_denominators_are_lopsided(capsys):
    """A biased pair must render warnings here too -- the guard is not silenced."""

    report = analyze_truncation_points.print_guard_comparison(
        {"n": 40, "fact_growth_tail": 36, "median_commands": 170},
        {"n": 4, "fact_growth_tail": 4, "median_commands": 54},
        window=10,
    )
    out = capsys.readouterr().out

    assert report is not None
    assert report.warnings
    assert report.causal_claim_allowed is False
    assert "!!" in out
    assert "不可直接解读为因果" in out


def test_empty_group_is_not_a_rate(capsys):
    report = analyze_truncation_points.print_guard_comparison(
        {"n": 5, "fact_growth_tail": 1, "median_commands": 10},
        None,
        window=10,
    )
    out = capsys.readouterr().out

    assert report is None
    assert "0/0" in out
    assert "没测量" in out


# ---------------------------------------------------------------------------
# 2. check_preregistration wired into prepare_fa_causal
# ---------------------------------------------------------------------------


def _complete_prereg(path: Path, **overrides) -> Path:
    payload = {
        "id": "demo-v1",
        "metrics": {
            "primary": {"name": "解出率"},
            "instrumentation": {"name": "muteki.exploit.first_action 出现率"},
        },
        "analysis": {"decision_rule": "实验组 >= 对照组 + 2 才推广"},
    }
    for dotted, value in overrides.items():
        head, _, tail = dotted.partition("__")
        payload[head] = value if not tail else {**payload.get(head, {}), tail: value}
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return path


def test_preregistered_file_passes(tmp_path, capsys):
    prereg = _complete_prereg(tmp_path / "ok.yaml")

    assert prepare_fa_causal.preregistration_gate(prereg, skip=False) is None
    out = capsys.readouterr().out
    assert "PASS" in out
    assert "主指标" in out


def test_incomplete_preregistration_refuses_and_names_the_missing_field(tmp_path, capsys):
    prereg = _complete_prereg(tmp_path / "bad.yaml", analysis__decision_rule="")

    assert prepare_fa_causal.preregistration_gate(prereg, skip=False) == 2
    out = capsys.readouterr().out
    assert "REFUSED" in out
    assert "analysis.decision_rule" in out
    assert "拒绝生成分组" in out


def test_missing_preregistration_file_refuses(tmp_path, capsys):
    assert prepare_fa_causal.preregistration_gate(tmp_path / "nope.yaml", skip=False) == 2
    assert "预注册文件不存在" in capsys.readouterr().out


def test_skip_switch_is_loud(tmp_path, capsys):
    prereg = _complete_prereg(tmp_path / "bad.yaml", analysis__decision_rule="")

    assert prepare_fa_causal.preregistration_gate(prereg, skip=True) is None
    out = capsys.readouterr().out
    assert "没有预注册" in out
    assert "不可作为因果证据" in out


def test_dry_run_is_gated_too(tmp_path, monkeypatch, capsys):
    """dry-run must not be a way around the gate: refusal happens before grouping."""

    prereg = _complete_prereg(tmp_path / "bad.yaml", analysis__decision_rule="")
    out_path = tmp_path / "grouping.json"
    monkeypatch.setattr(sys, "argv", [
        "prepare_fa_causal.py", "--dry-run",
        "--prereg", str(prereg), "--out", str(out_path),
    ])

    assert prepare_fa_causal.main() == 2
    assert "REFUSED" in capsys.readouterr().out
    assert not out_path.exists()


def test_dry_run_does_not_overwrite_the_locked_grouping(tmp_path, monkeypatch):
    """--dry-run used to write DEFAULT_OUT, silently replacing the real grouping."""

    locked = tmp_path / "fa-causal-v1.grouping.json"
    locked.write_text('{"locked": true}', encoding="utf-8")
    prereg = _complete_prereg(tmp_path / "ok.yaml")
    monkeypatch.setattr(prepare_fa_causal, "DEFAULT_OUT", locked)
    monkeypatch.setattr(prepare_fa_causal, "DEFAULT_PREREG", prereg)
    monkeypatch.setattr(sys, "argv", ["prepare_fa_causal.py", "--dry-run"])

    assert prepare_fa_causal.main() == 0
    assert json.loads(locked.read_text(encoding="utf-8")) == {"locked": True}
    assert (tmp_path / "fa-causal-v1.grouping.dryrun.json").is_file()


def test_prior_first_action_filter_uses_the_prefixed_event_name():
    """``exploit.first_action`` matches 0 rows, so the exclusion rule silently died."""

    source = (ROOT / "scripts" / "prepare_fa_causal.py").read_text(encoding="utf-8")
    assert "muteki.exploit.first_action" in source
    assert "event_type='exploit.first_action'" not in source


# ---------------------------------------------------------------------------
# 3. coverage_report wired into event_coverage
# ---------------------------------------------------------------------------


def test_default_targets_carry_the_muteki_prefix():
    assert all(name.startswith("muteki.") for name in event_coverage.DEFAULT_EXPECTED)
    assert "muteki.exploit.first_action" in event_coverage.DEFAULT_EXPECTED


def test_expect_parsing():
    assert event_coverage._parse_expect(["a", "b=0", "c=12"]) == {"a": None, "b": 0, "c": 12}
    with pytest.raises(ValueError, match="重复"):
        event_coverage._parse_expect(["a", "a"])
    with pytest.raises(ValueError, match="不是整数"):
        event_coverage._parse_expect(["a=x"])
    with pytest.raises(ValueError, match="不能为负"):
        event_coverage._parse_expect(["a=-1"])


def test_liveness_parsing_rejects_a_missing_equals(tmp_path):
    with pytest.raises(ValueError, match="CODE_PATH=EVENT_TYPE"):
        event_coverage._parse_liveness(["no-equals-here"])
    parsed = event_coverage._parse_liveness(["backend/app/x.py=muteki.a"])
    assert parsed[0][0] == ROOT / "backend" / "app" / "x.py"
    assert parsed[0][1] == "muteki.a"


def test_zero_events_are_flagged(monkeypatch, capsys):
    monkeypatch.setattr(
        event_coverage,
        "fetch_counts",
        lambda args, types: (
            {"muteki.exploit.first_action": 0, "muteki.worker_step": 12},
            "mysql fake",
        ),
    )
    code = event_coverage.main(
        ["--expect", "muteki.exploit.first_action", "--expect", "muteki.worker_step"]
    )
    out = capsys.readouterr().out

    assert code == event_coverage.EXIT_MISSING
    assert "ZERO-0事件" in out
    assert "muteki.exploit.first_action" in out


def test_unknown_event_name_is_called_out(monkeypatch, capsys):
    monkeypatch.setattr(
        event_coverage, "fetch_counts", lambda args, types: ({"muteki.worker_step": 3}, "fake")
    )
    code = event_coverage.main(["--expect", "muteki.worker_step", "--expect", "exploit.first_action"])
    out = capsys.readouterr().out

    assert code == event_coverage.EXIT_MISSING
    assert "表中未出现" in out
    assert "muteki." in out


def test_no_database_is_unknown_not_zero(monkeypatch, capsys):
    def boom(args, types):
        return None, "mysql://x:3399（连接失败: OperationalError）"

    monkeypatch.setattr(event_coverage, "fetch_counts", boom)
    code = event_coverage.main([])
    out = capsys.readouterr().out

    assert code == event_coverage.EXIT_NO_SOURCE
    assert "无法判定（无数据源）" in out
    # A zero would be a fake measurement: the table must not be rendered at all.
    assert "事件类型" not in out
    assert "ZERO" not in out


def test_liveness_without_data_source_is_not_a_pass(monkeypatch, capsys):
    monkeypatch.setattr(
        event_coverage, "fetch_counts", lambda args, types: ({"muteki.a": 1}, "fake")
    )

    def boom(args, event_type):
        raise RuntimeError("db down")

    monkeypatch.setattr(event_coverage, "fetch_last_event", boom)
    code = event_coverage.main([
        "--expect", "muteki.a",
        "--liveness", f"{ROOT / 'scripts' / 'event_coverage.py'}=muteki.a",
    ])
    out = capsys.readouterr().out

    assert code == event_coverage.EXIT_NO_SOURCE
    assert "UNKNOWN" in out
