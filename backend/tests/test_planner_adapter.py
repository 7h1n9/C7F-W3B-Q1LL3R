from app.solver.action import ActionIntent
from app.solver.blackboard import BlackboardState
from app.solver.planner import DeterministicPlanner
from app.solver.policy import ActionPolicyValidator, PolicyDecision


def _state(phase: str, actions: list[str | dict]) -> BlackboardState:
    return BlackboardState(
        run_id="planner-test",
        phase=phase,
        goal={"type": "SQL_INJECTION"},
        knowledge={"target_url": "http://target/search"},
        control={"allowed_actions": actions},
    )


def test_baseline_planner_can_only_select_http_request() -> None:
    state = _state("BASELINE", ["http_request"])
    intent = DeterministicPlanner().choose(state, ["http_request"])

    assert isinstance(intent, ActionIntent)
    assert intent.action_name == "http_request"
    assert intent.parameters == {"method": "GET", "url": "http://target/search"}


def test_validation_planner_selects_only_sql_boolean_compare() -> None:
    state = _state("VALIDATION", [{"name": "sql_boolean_compare", "purpose": "validate_boolean"}])
    intent = DeterministicPlanner().choose(
        state,
        [{"name": "sql_boolean_compare", "purpose": "validate_boolean"}],
    )

    assert intent is not None
    assert intent.action_name == "sql_boolean_compare"
    assert "sql_boolean_compare" not in {"content_discovery", "http_request"}


def test_policy_denies_action_outside_current_phase() -> None:
    validator = ActionPolicyValidator()
    intent = ActionIntent(
        action_name="content_discovery",
        reason="invalid baseline action",
    )

    result = validator.validate("BASELINE", intent)

    assert result.decision is PolicyDecision.DENY
    assert not result.allowed


def test_policy_allows_validation_and_exploitation_actions() -> None:
    validator = ActionPolicyValidator()

    validation = validator.validate("VALIDATION", ActionIntent("sql_boolean_compare", "validate"))
    extraction = validator.validate("EXPLOITATION", ActionIntent("data_extraction", "extract"))
    wrong_phase = validator.validate("VALIDATION", ActionIntent("content_discovery", "recon"))

    assert validation.decision is PolicyDecision.ALLOW
    assert extraction.decision is PolicyDecision.ALLOW
    assert wrong_phase.decision is PolicyDecision.DENY
