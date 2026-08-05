from app.services.payload_strategy import payload_strategy_manager, payload_family


def _args(**values):
    return {
        "payload_family": "and_boolean",
        "test_field": "asset_no",
        "baseline_value": "PC-2026-013",
        "true_condition": "' AND 1=1 -- ",
        "false_condition": "' AND 1=2 -- ",
        "request": {"method": "POST", "url": "http://target.test/check", "json": {"department": "OPS"}},
        **values,
    }


def test_same_payload_is_blocked_after_two_failures():
    history = []
    args = _args()
    first = payload_strategy_manager.attack_strategy_entry(
        history,
        vulnerability_type="SQL_INJECTION",
        target="asset_no",
        tool_name="sql_boolean_compare",
        payload_family_name="and_boolean",
        arguments=args,
        result="FAILURE",
        failure_reason="TRUE_SIDE_FAILED",
    )
    history.append(first)
    second = payload_strategy_manager.attack_strategy_entry(
        history,
        vulnerability_type="SQL_INJECTION",
        target="asset_no",
        tool_name="sql_boolean_compare",
        payload_family_name="and_boolean",
        arguments=args,
        result="FAILURE",
        failure_reason="TRUE_SIDE_FAILED",
    )
    history.append(second)

    assert second["attempts"] == 2
    assert second["status"] == "BLOCKED"
    assert payload_strategy_manager.is_attack_strategy_blocked(
        history, vulnerability_type="SQL_INJECTION", target="asset_no", arguments=args
    )


def test_different_payload_remains_allowed():
    history = []
    original = _args()
    history.append(payload_strategy_manager.attack_strategy_entry(
        history,
        vulnerability_type="SQL_INJECTION",
        target="asset_no",
        tool_name="sql_boolean_compare",
        payload_family_name="and_boolean",
        arguments=original,
        result="FAILURE",
        failure_reason="TRUE_SIDE_FAILED",
    ))
    changed = _args(true_condition="' AND (1=1) -- ")
    assert not payload_strategy_manager.is_attack_strategy_blocked(
        history, vulnerability_type="SQL_INJECTION", target="asset_no", arguments=changed
    )


def test_different_strategy_family_is_allowed():
    history = []
    and_args = _args(payload_family="and_boolean")
    history.extend([
        payload_strategy_manager.attack_strategy_entry(
            history,
            vulnerability_type="SQL_INJECTION",
            target="asset_no",
            tool_name="sql_boolean_compare",
            payload_family_name="and_boolean",
            arguments=and_args,
            result="FAILURE",
            failure_reason="TRUE_SIDE_FAILED",
        )
    ])
    history.append(payload_strategy_manager.attack_strategy_entry(
        history,
        vulnerability_type="SQL_INJECTION",
        target="asset_no",
        tool_name="sql_boolean_compare",
        payload_family_name="and_boolean",
        arguments=and_args,
        result="FAILURE",
        failure_reason="TRUE_SIDE_FAILED",
    ))
    or_args = _args(payload_family="or_boolean", true_condition="' OR 1=1 -- ", false_condition="' OR 1=2 -- ")
    assert payload_family("sql_boolean_compare", or_args) == "or_boolean"
    assert not payload_strategy_manager.is_attack_strategy_blocked(
        history, vulnerability_type="SQL_INJECTION", target="asset_no", arguments=or_args
    )
