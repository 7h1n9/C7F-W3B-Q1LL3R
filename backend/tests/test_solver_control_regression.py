from types import SimpleNamespace

import pytest

from app.orchestration.state_machine import RunStatus, transition
from app.services.action_quality import ActionQualityGate, RecoveryPlanner
from app.services.terminal_outcomes import TerminalOutcomeResolver


def test_terminal_outcome_verified_flag_dominates_stream_error() -> None:
    resolver = TerminalOutcomeResolver()
    assert resolver.resolve("FAILED_ENGINE", flag_verified=True, stream_error=True) == "VERIFIED_FLAG"
    assert resolver.resolve("COMPLETED_SOLVED", stream_error=True) == "COMPLETED_SOLVED"


def test_solved_run_cannot_be_overwritten_or_resumed() -> None:
    run = SimpleNamespace(status="COMPLETED_SOLVED", current_phase="COMPLETED_SOLVED")
    with pytest.raises(Exception):
        transition(run, RunStatus.FAILED_ENGINE)


def test_action_quality_escalates_degraded_defaults() -> None:
    gate = ActionQualityGate()
    action = {"type": "tool", "objective": "Continue the authorized investigation", "hypothesis": "Initial investigation hypothesis"}
    state = {"current_phase": "INTAKE", "confirmed_facts": [{"source": "http"}]}
    assert gate.evaluate(action, state).action == "REPAIR_ACTION"
    state["degraded_action_streak"] = 1
    assert gate.evaluate(action, state).action == "PlanAction"
    state["degraded_action_streak"] = 2
    assert gate.evaluate(action, state).action == "RecoveryPlanner"


def test_recovery_planner_escalates_duplicate_actions() -> None:
    assert RecoveryPlanner().plan(phase="TESTING", no_progress=2, duplicate_streak=3)["action"] == "AutomationAction"
