"""Tests for the metrics-discipline guardrails.

Every timestamp is fixed.  A guardrail test that depends on the wall clock can
pass for the wrong reason at 23:59 and fail at 00:01, which is the same class of
mistake the guardrail exists to prevent.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services.metrics_guard import (
    ComparisonBiasRisk,
    FeatureNeverRan,
    MetricsDataUnavailable,
    PreregistrationIncomplete,
    assert_feature_live,
    check_preregistration,
    coverage_report,
    format_comparison,
    judge_feature_live,
    require_causal_comparison,
    snapshot,
)

UTC = timezone.utc
REPO_ROOT = Path(__file__).resolve().parents[2]
FA_EXPERIMENT = REPO_ROOT / "experiments" / "fa-causal-v1.yaml"

CODE_MTIME = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# 2. feature liveness
# --------------------------------------------------------------------------


def test_code_newer_than_last_event_fails() -> None:
    verdict = judge_feature_live(
        "exploit.first_action",
        event_times=[datetime(2026, 11, 18, 5, 0, tzinfo=UTC)],
        code_mtime=CODE_MTIME,
        code_path="backend/app/solver/muteki/coordinator.py",
    )

    assert verdict.status == "NEVER_RAN"
    assert verdict.ok is False
    assert verdict.lag_hours == pytest.approx(5.0)
    assert "从未执行过" in verdict.message
    assert "不要基于此出数" in verdict.message


def test_event_newer_than_code_passes() -> None:
    verdict = judge_feature_live(
        "exploit.first_action",
        event_times=[
            datetime(2026, 11, 18, 11, 0, tzinfo=UTC),
            datetime(2026, 11, 18, 12, 30, tzinfo=UTC),
        ],
        code_mtime=CODE_MTIME,
    )

    assert verdict.status == "LIVE"
    assert verdict.ok is True
    assert verdict.event_count == 2
    assert verdict.last_event_at == datetime(2026, 11, 18, 12, 30, tzinfo=UTC)


def test_zero_events_is_never_ran_not_unknown() -> None:
    verdict = judge_feature_live("exploit.first_action", event_times=[], code_mtime=CODE_MTIME)

    assert verdict.status == "NEVER_RAN"
    assert verdict.event_count == 0
    assert "0 条" in verdict.message


def test_missing_data_source_is_unknown_not_pass() -> None:
    verdict = judge_feature_live("exploit.first_action", event_times=None, code_mtime=CODE_MTIME)

    assert verdict.status == "UNKNOWN"
    assert verdict.ok is False
    assert "无法判定" in verdict.message
    assert "无数据源" in verdict.message


def test_assert_raises_in_both_directions() -> None:
    with pytest.raises(FeatureNeverRan):
        assert_feature_live(
            "exploit.first_action",
            event_times=[datetime(2026, 11, 17, 9, 0, tzinfo=UTC)],
            code_mtime=CODE_MTIME,
        )

    passed = assert_feature_live(
        "exploit.first_action",
        event_times=[datetime(2026, 11, 18, 11, 0, tzinfo=UTC)],
        code_mtime=CODE_MTIME,
    )
    assert passed.ok is True


def test_assert_treats_unknown_as_failure() -> None:
    with pytest.raises(MetricsDataUnavailable):
        assert_feature_live("exploit.first_action", event_times=None, code_mtime=CODE_MTIME)

    lenient = assert_feature_live(
        "exploit.first_action", event_times=None, code_mtime=CODE_MTIME, strict=False
    )
    assert lenient.status == "UNKNOWN"


def test_naive_timestamps_are_read_as_utc() -> None:
    """MySQL DATETIME columns come back naive; mixing them with aware values must not crash."""

    verdict = judge_feature_live(
        "exploit.first_action",
        event_times=[datetime(2026, 11, 18, 11, 0)],
        code_mtime=CODE_MTIME,
    )

    assert verdict.status == "LIVE"


# --------------------------------------------------------------------------
# 3. comparison guardrail
# --------------------------------------------------------------------------


def test_real_asymmetric_numbers_always_warn() -> None:
    """4/122 vs 127/142 is the comparison that was misread as causal."""

    report = format_comparison("ACTIVE 中有 FA", 4, 122, "SOLVED 中有 FA", 127, 142)

    assert report.has_bias_risk is True
    assert "选择偏差" in report.text
    assert "不可直接解读为因果" in report.text
    # Raw numbers, both rates and both ratios must be on screen.
    assert "4/122" in report.text
    assert "127/142" in report.text
    assert "3.28%" in report.text
    assert "89.44%" in report.text
    assert "31.8" in report.text
    assert report.causal_claim_allowed is False
    print("\n" + report.text)


def test_balanced_comparison_does_not_warn() -> None:
    report = format_comparison("treatment", 40, 100, "control", 35, 98)

    assert report.has_bias_risk is False
    assert "选择偏差风险" not in report.text
    assert "40/100" in report.text and "35/98" in report.text
    assert report.causal_claim_allowed is True


def test_small_denominator_warns() -> None:
    report = format_comparison("treatment", 2, 4, "control", 25, 100)

    assert report.has_bias_risk is True
    assert any("小于 10" in item for item in report.warnings)


def test_large_sample_ratio_warns() -> None:
    report = format_comparison("treatment", 5, 12, "control", 60, 120)

    assert any("样本量相差" in item for item in report.warnings)


def test_override_requires_reason_and_keeps_warning() -> None:
    with pytest.raises(ValueError, match="override_reason"):
        format_comparison("A", 4, 122, "B", 127, 142, override=True)

    report = format_comparison(
        "A", 4, 122, "B", 127, 142, override=True, override_reason="仅用于说明分组不可比"
    )
    assert "仅用于说明分组不可比" in report.text
    assert "不可直接解读为因果" in report.text  # the guardrail is still visible
    assert report.causal_claim_allowed is True


def test_override_is_audited(tmp_path: Path) -> None:
    audit = tmp_path / "overrides.jsonl"
    format_comparison(
        "A",
        4,
        122,
        "B",
        127,
        142,
        override=True,
        override_reason="历史表格复述",
        audit_path=audit,
    )

    rows = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["kind"] == "comparison_override"
    assert rows[0]["override_reason"] == "历史表格复述"
    assert rows[0]["comparison"]["numerator_b"] == 127


def test_zero_denominator_is_rejected() -> None:
    with pytest.raises(ValueError, match="分母"):
        format_comparison("A", 0, 0, "B", 5, 10)


def test_causal_gate_blocks_biased_report() -> None:
    report = format_comparison("A", 4, 122, "B", 127, 142)

    with pytest.raises(ComparisonBiasRisk):
        require_causal_comparison(report)


# --------------------------------------------------------------------------
# 4. preregistration
# --------------------------------------------------------------------------


def test_repo_experiment_passes_preregistration() -> None:
    check = check_preregistration(FA_EXPERIMENT)

    assert check.ok is True
    assert check.experiment_id == "fa-causal-v1"
    assert check.missing == ()
    assert "PASS" in check.text
    assert "exploit.first_action" in check.text


def test_missing_instrumentation_metric_refuses_experiment(tmp_path: Path) -> None:
    experiment = tmp_path / "half-baked.yaml"
    experiment.write_text(
        "\n".join(
            [
                "id: half-baked",
                "metrics:",
                "  primary:",
                '    name: "解出率"',
                "analysis:",
                '  decision_rule: "看感觉"',
            ]
        ),
        encoding="utf-8",
    )

    check = check_preregistration(experiment)
    assert check.ok is False
    assert "metrics.instrumentation.name" in check.missing
    assert "REFUSED" in check.text

    with pytest.raises(PreregistrationIncomplete):
        check_preregistration(experiment, strict=True)


def test_missing_decision_rule_refuses_experiment(tmp_path: Path) -> None:
    experiment = tmp_path / "no-rule.yaml"
    experiment.write_text(
        "\n".join(
            [
                "id: no-rule",
                "metrics:",
                "  primary:",
                '    name: "解出率"',
                "  instrumentation:",
                '    name: "exploit.first_action"',
            ]
        ),
        encoding="utf-8",
    )

    check = check_preregistration(experiment)
    assert check.ok is False
    assert "analysis.decision_rule" in check.missing


# --------------------------------------------------------------------------
# 5. coverage
# --------------------------------------------------------------------------


def test_zero_event_type_is_marked() -> None:
    report = coverage_report(
        {"exploit.first_action": None, "run.finished": 5},
        {"run.finished": 5},
    )

    assert report.zero == ("exploit.first_action",)
    assert report.missing == ("exploit.first_action",)
    assert "ZERO-0事件" in report.text
    assert "实际为 0" in report.text
    assert "run.finished" not in report.zero


def test_all_covered_is_ok() -> None:
    report = coverage_report({"exploit.first_action": None}, {"exploit.first_action": 7})

    assert report.ok is True
    assert report.zero == ()


# --------------------------------------------------------------------------
# 1. snapshots
# --------------------------------------------------------------------------


def test_snapshot_appends_recomputable_rows(tmp_path: Path) -> None:
    log = tmp_path / "nested" / "snapshots.jsonl"
    first = snapshot(
        sql_or_source="SELECT COUNT(*) FROM run_events WHERE event_type=%s",
        filters={"event_type": "exploit.first_action"},
        code_mtime="2026-11-18T10:00:00Z",
        data_window="2026-11-01..2026-11-18",
        result={"count": 0},
        path=log,
    )
    second = snapshot(
        sql_or_source="SELECT status, COUNT(*) FROM solve_runs GROUP BY status",
        filters={},
        code_path=FA_EXPERIMENT,
        result=[{"status": "ACTIVE", "count": 122}],
        path=log,
        computed_at=datetime(2026, 11, 18, 12, 0, tzinfo=UTC),
    )

    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert first.query_id != second.query_id
    assert rows[0]["filters"] == {"event_type": "exploit.first_action"}
    assert rows[0]["result"] == {"count": 0}
    assert rows[0]["computed_at"].endswith("Z")
    assert rows[1]["computed_at"] == "2026-11-18T12:00:00Z"
    assert rows[1]["code_mtime"].endswith("Z")

    for key in (
        "query_id",
        "sql_or_source",
        "filters",
        "code_mtime",
        "data_window",
        "computed_at",
        "result",
    ):
        assert key in rows[0]


def test_snapshot_refuses_to_lose_provenance() -> None:
    with pytest.raises(ValueError, match="sql_or_source"):
        snapshot(sql_or_source="  ", result=1, code_mtime="2026-11-18T10:00:00Z", path=None)

    with pytest.raises(ValueError, match="code_mtime"):
        snapshot(sql_or_source="SELECT 1", result=1, path=None)


def test_snapshot_can_derive_mtime_from_code(tmp_path: Path) -> None:
    code = tmp_path / "coordinator.py"
    code.write_text("# stub\n", encoding="utf-8")

    record = snapshot(sql_or_source="SELECT 1", result=None, code_path=code, path=None)

    assert record.code_mtime is not None
    assert record.code_mtime.endswith("Z")
    stamp = datetime.fromisoformat(record.code_mtime.replace("Z", "+00:00"))
    assert abs(stamp - datetime.now(tz=UTC)) < timedelta(minutes=5)
