"""Pass@k framing: make this project's numbers comparable to CTF-Dojo's.

CTF-Dojo reports Pass@1 over episodes with a per-challenge interaction budget.
The evaluator previously only produced SUCCESS / PARTIAL / INCOMPLETE, so a Run
that solved the case in three turns and one that solved it in sixty were
indistinguishable, and nothing could be lined up against an external number.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.benchmark.case_definition import BenchmarkCase
from app.benchmark.case_loader import load_case
from app.benchmark.evaluation import aggregate_pass_at_k, evaluate_run


def _solved_snapshot(**overrides) -> dict:
    snapshot = {
        "run_id": "run-pass-k",
        "events": [],
        "security_context": {},
        "flag_candidates": [
            {"id": "flag-1", "verified": True, "review_state": "VALID"},
        ],
    }
    snapshot.update(overrides)
    return snapshot


def test_report_records_the_passk_fields_without_a_budget() -> None:
    # Cleared explicitly rather than by picking a fixture that happens to lack a
    # budget, so this stays a test of the no-budget path and not of whatever the
    # shipped cases currently declare.
    case = load_case("sql-injection-boolean").model_copy(update={"turn_budget": None})
    report = evaluate_run(case, _solved_snapshot())

    assert report["final_result"] == "SUCCESS"
    assert report["attempt"] == 1
    assert report["turns_used"] == 0
    assert report["verified_flag_rank"] == 1
    # No declared budget means the answer is genuinely unknown, not "failed".
    assert report["turn_budget"] is None
    assert report["solved_within_budget"] is None


def test_solved_within_budget_compares_turns_against_the_case_budget() -> None:
    case = load_case("sql-injection-boolean").model_copy(update={"turn_budget": 2})

    within = evaluate_run(case, _solved_snapshot(agent_turns=[{}, {}]))
    over = evaluate_run(case, _solved_snapshot(agent_turns=[{}, {}, {}]))

    assert within["turn_budget"] == 2
    assert within["turns_used"] == 2
    assert within["solved_within_budget"] is True
    assert over["turns_used"] == 3
    assert over["solved_within_budget"] is False


def test_an_unsolved_run_is_never_within_budget() -> None:
    case = load_case("sql-injection-boolean").model_copy(update={"turn_budget": 40})
    report = evaluate_run(case, {"run_id": "r", "events": [], "security_context": {}})

    assert report["final_result"] == "INCOMPLETE"
    assert report["solved_within_budget"] is False
    assert report["verified_flag_rank"] is None


def test_verified_flag_rank_counts_rejected_submissions() -> None:
    case = load_case("sql-injection-boolean")
    report = evaluate_run(
        case,
        _solved_snapshot(
            flag_candidates=[
                {"id": "flag-1", "verified": False, "review_state": "INVALID"},
                {"id": "flag-2", "verified": False, "review_state": "PENDING"},
                {"id": "flag-3", "verified": True, "review_state": "VALID"},
            ]
        ),
    )

    assert report["verified_flag_rank"] == 3
    assert report["final_result"] == "SUCCESS"


def test_turn_budget_must_be_a_positive_integer() -> None:
    payload = {
        "case_id": "unit-case",
        "vulnerability_type": "SQL_INJECTION",
        "target": "http://target.test/",
    }
    assert BenchmarkCase.model_validate({**payload, "turn_budget": 40}).turn_budget == 40
    assert BenchmarkCase.model_validate(payload).turn_budget is None

    with pytest.raises(ValidationError):
        BenchmarkCase.model_validate({**payload, "turn_budget": 0})
    with pytest.raises(ValidationError):
        BenchmarkCase.model_validate({**payload, "turn_budget": "forty"})


def test_aggregate_never_invents_a_number_from_nothing() -> None:
    assert aggregate_pass_at_k([]) == {
        "attempts": 0,
        "pass_at_1": None,
        "success_within_budget": None,
        "mean_turns_used": None,
    }


def test_aggregate_reports_pass_at_1_and_budget_success() -> None:
    reports = [
        {"final_result": "SUCCESS", "turns_used": 4, "solved_within_budget": True},
        {"final_result": "SUCCESS", "turns_used": 8, "solved_within_budget": False},
        {"final_result": "PARTIAL", "turns_used": 6, "solved_within_budget": False},
        {"final_result": "INCOMPLETE", "turns_used": 6, "solved_within_budget": False},
    ]

    summary = aggregate_pass_at_k(reports)

    assert summary["attempts"] == 4
    assert summary["pass_at_1"] == pytest.approx(0.5)
    assert summary["success_within_budget"] == pytest.approx(0.25)
    assert summary["mean_turns_used"] == pytest.approx(6.0)


def test_aggregate_ignores_unknown_budgets_instead_of_counting_them_as_failures() -> None:
    reports = [
        {"final_result": "SUCCESS", "turns_used": 3, "solved_within_budget": None},
        {"final_result": "SUCCESS", "turns_used": 5, "solved_within_budget": True},
    ]

    summary = aggregate_pass_at_k(reports)

    assert summary["attempts"] == 2
    assert summary["pass_at_1"] == pytest.approx(1.0)
    assert summary["success_within_budget"] == pytest.approx(1.0)
