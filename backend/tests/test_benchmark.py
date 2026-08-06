import json

from app.benchmark.case_loader import load_case, load_cases
from app.benchmark.evaluation import evaluate_run


def test_three_benchmark_cases_load():
    cases = load_cases()

    assert {case.case_id for case in cases} == {
        "sql-injection-boolean",
        "xss-reflected",
        "ssrf-internal",
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
        "final_result": "SUCCESS",
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
    assert report["metrics"]["reasoning"] == {
        "strategy_migrations": 1,
        "feedback_events": 1,
        "failed_attempts": 1,
        "duplicate_experiments": 1,
    }
    assert [item["evidence_ids"] for item in report["evidence_chain"]] == [["e1"], ["e2"], ["e3"], ["e4"]]
    assert persisted["case_id"] == "sql-injection-boolean"
    assert len(persisted["timeline"]) == 4
