import json
from types import SimpleNamespace

import pytest

import app.benchmark.evaluation as evaluation_module
from app.benchmark.case_loader import load_case, load_cases
from app.benchmark.evaluation import evaluate_run, evaluate_session


def test_the_shipped_benchmark_cases_load():
    """Pins the shipped case set, so a case that stops parsing is noticed.

    The name deliberately does not say "three": a fourth case was added
    (``vulhub-tomcat-put``, CVE-2017-12615 against the local vulhub range) and
    the count-based name made the failure look like a broken loader rather than
    an expected change.
    """

    cases = load_cases()

    assert {case.case_id for case in cases} == {
        "sql-injection-boolean",
        "sql-injection-golden",
        "xss-reflected",
        "ssrf-internal",
        "vulhub-tomcat-put",
    }
    assert load_case("sql-injection-boolean").expected_validation["status"] == "VALIDATED"


def test_evaluation_records_agent_effect_efficiency_and_reasoning_metrics(tmp_path):
    case = load_case("sql-injection-boolean")
    snapshot = {
        "run_id": "run-benchmark-1",
        "target": case.target,
        "agent_tasks": [
            {"agent_role": "PLANNER", "task_kind": "PLANNING"},
            {"agent_role": "EXPLOIT", "task_kind": "EXPLOIT"},
        ],
        "agent_turns": [{"input_tokens": 12, "output_tokens": 8}],
        "tool_calls": [{"tool_name": "http_request"}, {"tool_name": "sql_boolean_compare"}],
        "planner_proposals": [{"current_stage": "BOOLEAN_ORACLE"}],
        "events": [
            {"sequence": 1, "event_type": "strategy.feedback.created", "classification": "TRUE_SIDE_FAILED"},
            {"sequence": 2, "event_type": "strategy.migration.applied", "from": "BOOLEAN_AND", "to": "BOOLEAN_OR"},
            {"sequence": 3, "event_type": "tool.failed"},
            {"sequence": 4, "event_type": "experiment.duplicate_rejected"},
        ],
        "security_context": {
            "hypotheses": [{"id": "h1", "status": "OPEN"}],
            "validation_results": [{"status": "VALIDATED", "evidence_ids": ["e1"]}],
            "exploit_results": [{"status": "SUCCESS", "evidence_ids": ["e2"]}],
            "impact_assessments": [{"status": "CONFIRMED", "evidence_ids": ["e3"]}],
            "findings": [{"status": "CREATED", "evidence_ids": ["e4"]}],
        },
    }

    report = evaluate_run(case, snapshot, tmp_path)
    persisted = json.loads((tmp_path / "run_report.json").read_text(encoding="utf-8"))

    assert report["final_result"] == "SUCCESS"
    assert report["metrics"]["agent"] == {"agent_calls": 1, "tool_calls": 2, "planner_calls": 1}
    assert report["metrics"]["effect"] == {
        "vulnerability_discovered": True,
        "validation_success": True,
        "exploit_complete": True,
        "finding_created": True,
    }
    assert report["metrics"]["efficiency"]["total_tokens"] == 20
    assert report["validation_success"] is True
    assert report["exploit_success"] is True
    assert report["impact_confirmed"] is True
    assert report["finding_created"] is True
    assert report["evaluator"] == {
        "name": "c7f-benchmark-evaluator",
        "schema_version": 1,
        "run_id": "run-benchmark-1",
        "basis": "durable_run_evidence",
        "evidence": {
            "verified_flag_candidates": 0,
            "finding_created": True,
        },
    }
    assert report["metrics"]["reasoning"] == {
        "strategy_migrations": 1,
        "feedback_events": 1,
        "failed_attempts": 1,
        "duplicate_experiments": 1,
    }
    assert [item["evidence_ids"] for item in report["evidence_chain"]] == [["e1"], ["e2"], ["e3"], ["e4"]]
    assert persisted["case_id"] == "sql-injection-boolean"
    assert len(persisted["timeline"]) == 4


def test_evaluation_does_not_treat_lifecycle_status_as_success():
    case = load_case("sql-injection-boolean")
    report = evaluate_run(
        case,
        {
            "run_id": "run-status-only",
            "status": "COMPLETED_SOLVED",
            "events": [],
            "security_context": {},
        },
    )

    assert report["final_result"] == "INCOMPLETE"
    assert report["evaluator"]["run_id"] == "run-status-only"
    assert report["evaluator"]["basis"] == "durable_run_evidence"


def test_verified_flag_candidate_is_durable_success_evidence():
    case = load_case("sql-injection-boolean")
    report = evaluate_run(
        case,
        {
            "run_id": "run-verified-flag",
            "events": [],
            "security_context": {},
            "flag_candidates": [
                {
                    "id": "flag-1",
                    "verified": True,
                    "review_state": "VALID",
                }
            ],
        },
    )

    assert report["final_result"] == "SUCCESS"
    assert report["evaluator"]["evidence"]["verified_flag_candidates"] == 1


@pytest.mark.asyncio
async def test_evaluate_session_does_not_pass_lifecycle_status_as_final_result(
    monkeypatch,
):
    captured: dict = {}

    class ScalarResult:
        def all(self):
            return []

    class FakeSession:
        async def get(self, _model, run_id):
            return SimpleNamespace(
                id=run_id,
                status="COMPLETED_SOLVED",
                started_at=None,
                finished_at=None,
            )

        async def scalars(self, _statement):
            return ScalarResult()

        async def scalar(self, _statement):
            return None

    def fake_evaluate(_case, snapshot, _output_dir=None):
        captured["snapshot"] = snapshot
        return {"final_result": "INCOMPLETE"}

    monkeypatch.setattr(evaluation_module, "evaluate_run", fake_evaluate)

    await evaluate_session(
        FakeSession(),
        "run-without-evidence",
        load_case("sql-injection-boolean"),
    )

    assert captured["snapshot"]["status"] == "COMPLETED_SOLVED"
    assert "final_result" not in captured["snapshot"]
