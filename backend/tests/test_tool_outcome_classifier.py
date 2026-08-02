from app.services.tool_outcome_classifier import ToolOutcome, classify_tool_outcome


def test_completed_empty_metadata_is_no_fact():
    assert classify_tool_outcome({"status": "NO_FACT", "error_code": "MYSQL_METADATA_EMPTY_RESULT"}) is ToolOutcome.NO_FACT


def test_result_contract_is_not_retryable():
    assert classify_tool_outcome({"status": "FAILED", "stage": "RESULT_CONTRACT"}) is ToolOutcome.CONTRACT_ERROR


def test_contract_stage_wins_over_legacy_empty_error_code():
    assert classify_tool_outcome({
        "status": "FAILED",
        "stage": "RESULT_CONTRACT",
        "error_code": "MYSQL_METADATA_EMPTY_RESULT",
    }) is ToolOutcome.CONTRACT_ERROR


def test_timeout_and_network_are_retryable():
    assert classify_tool_outcome({"status": "FAILED", "error": "network timeout"}) is ToolOutcome.RETRYABLE_ERROR


def test_unknown_failure_is_infrastructure_error():
    assert classify_tool_outcome({"status": "FAILED", "error_code": "ORACLE_UNAVAILABLE"}) is ToolOutcome.INFRA_ERROR
