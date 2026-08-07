from app.security.attack_state_engine import (
    AttackStateEngine,
    validate_attack_state_selection,
)


def _boolean_entry(variant: str, experiment_id: str) -> dict:
    return {
        "experiment_id": experiment_id,
        "vulnerability_type": "SQL_INJECTION",
        "strategy_family": "BOOLEAN",
        "strategy_variant": variant,
        "status": "INCONCLUSIVE",
        "result_classification": "TRUE_SIDE_FAILED",
    }


def test_boolean_failure_migrates_to_allowed_variant_actions():
    state = AttackStateEngine().evaluate(
        diagnosis={
            "strategy": "BOOLEAN_AND",
            "classification": "TRUE_SIDE_FAILED",
            "recommended_strategy": ["BOOLEAN_AND_COMMENT_HASH", "BOOLEAN_AND_ENCODING"],
        },
        strategy_history=[_boolean_entry("AND", "and-1")],
    )

    assert state.current_phase == "BOOLEAN_CALIBRATION"
    assert "BOOLEAN_COMMENT_HASH" in state.available_actions
    assert "BOOLEAN_ENCODING" in state.available_actions
    assert "BOOLEAN_AND" not in state.available_actions
    assert state.required_transition == "CHANGE_BOOLEAN_VARIANT"


def test_boolean_family_exhaustion_changes_attack_family():
    state = AttackStateEngine().evaluate(
        diagnosis={
            "family": "BOOLEAN",
            "classification": "NO_SIGNAL",
            "exhausted": True,
        },
        strategy_history=[
            _boolean_entry("AND", "and-1"),
            _boolean_entry("AND_COMMENT_HASH", "hash-1"),
            _boolean_entry("AND_ENCODING", "encoding-1"),
        ],
    )

    assert state.required_transition == "CHANGE_ATTACK_FAMILY"
    assert state.available_actions == ["ERROR_BASED", "UNION_BASED", "TIME_BASED"]


def test_planner_action_outside_attack_state_is_rejected():
    result = validate_attack_state_selection(
        {
            "available_actions": ["BOOLEAN_OR"],
            "required_transition": "CHANGE_BOOLEAN_VARIANT",
        },
        {"strategy_family": "BOOLEAN", "strategy_variant": "AND"},
    )

    assert result["valid"] is False
    assert result["reason"] == "ATTACK_ACTION_NOT_ALLOWED"


def test_planner_action_inside_attack_state_is_allowed():
    result = validate_attack_state_selection(
        {"available_actions": ["BOOLEAN_OR"]},
        {"strategy_family": "BOOLEAN", "strategy_variant": "OR"},
    )

    assert result["valid"] is True


def test_validated_security_context_enters_exploitation_action_space():
    state = AttackStateEngine().evaluate(
        security_context={
            "validation_results": [
                {"type": "SQL_INJECTION", "status": "VALIDATED", "confidence": 0.95}
            ]
        }
    )

    assert state.current_phase == "EXPLOITATION"
    assert state.available_actions == ["METADATA_EXTRACTION", "DATA_EXTRACTION"]
