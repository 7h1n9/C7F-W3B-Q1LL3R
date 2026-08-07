from app.orchestration.state_machine import RunStatus
from app.solver.lifecycle import (
    LifecycleDecision,
    SolverLifecycleMapper,
    SolverLifecycleOutcome,
)

EXPECTED = {
    SolverLifecycleOutcome.CONTINUE: RunStatus.RUNNING,
    SolverLifecycleOutcome.APPROVAL_REQUIRED: RunStatus.WAITING_USER,
    SolverLifecycleOutcome.USER_INPUT_REQUIRED: RunStatus.WAITING_USER,
    SolverLifecycleOutcome.RECOVERABLE_WORKER_FAILURE: RunStatus.RUNNING,
    SolverLifecycleOutcome.ENGINE_EXCEPTION: RunStatus.FAILED_ENGINE,
    SolverLifecycleOutcome.TIMEOUT: RunStatus.TIMEOUT,
    SolverLifecycleOutcome.COMPLETION_SOLVED: RunStatus.COMPLETED_SOLVED,
    SolverLifecycleOutcome.COMPLETION_UNSOLVED: RunStatus.COMPLETED_UNSOLVED,
}


def test_all_solver_outcomes_map_to_expected_run_statuses():
    mapper = SolverLifecycleMapper()

    for outcome, expected_status in EXPECTED.items():
        decision = mapper.map(outcome)

        assert isinstance(decision, LifecycleDecision)
        assert decision.target_status is expected_status
        assert decision.reason_code == outcome.value


def test_mapper_does_not_mutate_input_or_hold_runtime_state():
    mapper = SolverLifecycleMapper()
    outcome = SolverLifecycleOutcome.COMPLETION_UNSOLVED

    decision = mapper.map(outcome)

    assert outcome is SolverLifecycleOutcome.COMPLETION_UNSOLVED
    assert decision == LifecycleDecision(RunStatus.COMPLETED_UNSOLVED, "COMPLETION_UNSOLVED")
    assert not hasattr(mapper, "blackboard")
    assert not hasattr(mapper, "evidence_authority")


def test_solved_mapping_is_only_a_projection_and_does_not_evaluate_completion():
    mapper = SolverLifecycleMapper()

    decision = mapper.map(SolverLifecycleOutcome.COMPLETION_SOLVED)

    assert decision.target_status is RunStatus.COMPLETED_SOLVED
    assert decision.reason_code == "COMPLETION_SOLVED"
    assert not hasattr(mapper, "evaluate")
