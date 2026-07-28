import pytest

from app.core.exceptions import DomainError
from app.orchestration.state_machine import RunStatus, restart, transition
from app.services.sql_provenance import validate_sql_expression_provenance
from app.services.workspace_policy import searchable_path
from app.services.effective_logical_tool_calls import EffectiveLogicalToolCallService


def test_pause_and_failure_do_not_become_solver_phases() -> None:
    class Run:
        status = "EXECUTING"
        current_phase = "FLAG_SEARCH"
        started_at = None
        finished_at = None

    run = Run()
    transition(run, RunStatus.PAUSED_DEPLOYMENT)
    assert run.status == "PAUSED_DEPLOYMENT"
    assert run.current_phase == "FLAG_SEARCH"
    previous = restart(run)
    assert previous == RunStatus.PAUSED_DEPLOYMENT
    assert run.current_phase == "FLAG_SEARCH"


def test_logical_id_collision_is_detectable() -> None:
    value = EffectiveLogicalToolCallService.build_mcp_id("r", "a", "t", "p")
    assert value == "mcp:r:a:t:p"
    with pytest.raises(DomainError):
        EffectiveLogicalToolCallService.build_mcp_id("r", "a", "t", "provider:bad")


def test_sql_expression_requires_structured_sources() -> None:
    with pytest.raises(DomainError, match="SQL_EXPRESSION_PROVENANCE_REQUIRED"):
        validate_sql_expression_provenance({"target_expression": "SELECT value FROM config"})
    with pytest.raises(DomainError, match="SQL_EXPRESSION_PROVENANCE_REQUIRED"):
        validate_sql_expression_provenance({"target_expression": "SELECT value FROM config", "expression_type": "VALUE_EXTRACTION", "supporting_evidence_ids": ["e"], "supporting_fact_ids": ["f"], "source_hypothesis_id": "h", "approved_analysis_review_id": "r", "assumption_status": "VERIFIED"})


def test_workspace_search_excludes_runtime_and_old_outputs() -> None:
    assert searchable_path("attachments/request.txt")
    assert searchable_path("final/draft/report.md")
    assert not searchable_path(".jobs/job.json")
    assert not searchable_path("outputs/runner.json")
    assert not searchable_path("final/verified/flag.txt")
