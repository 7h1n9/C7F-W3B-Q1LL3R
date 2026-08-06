from app.services.boolean_oracle_failure_analyzer import (
    analyze_boolean_oracle,
    apply_boolean_strategy_variant,
)
from app.services.experiment_result_classifier import ExperimentResultClassifier
from app.services.experiment_strategy_manager import experiment_strategy_manager


def _payload(**values):
    return {
        "stable_true": False,
        "stable_false": True,
        "response_differential": False,
        "boolean_oracle_confirmed": False,
        "true_signature": {"matched": False},
        "false_signature": {"matched": False},
        "oracle": {"json_field": "matched", "true_value": True, "false_value": False},
        "request_contract": {"method": "POST", "url": "http://asset.local/check"},
        "test_field": "asset_no",
        "true_condition": "' AND 1=1 -- ",
        "false_condition": "' AND 1=2 -- ",
        **values,
    }


def test_true_side_failure_explains_payload_and_recommends_variants():
    diagnosis = analyze_boolean_oracle(_payload())

    assert diagnosis["classification"] == "TRUE_SIDE_FAILED"
    assert "payload_not_reflected" in diagnosis["reason"]
    assert "comment_failed" in diagnosis["reason"]
    assert diagnosis["recommended_strategy"][:2] == [
        "BOOLEAN_AND_COMMENT_HASH",
        "BOOLEAN_AND_COMMENT_INLINE",
    ]


def test_boolean_comment_variation_allowed():
    original = {
        "true_condition": "' AND 1=1 -- ",
        "false_condition": "' AND 1=2 -- ",
    }
    varied = apply_boolean_strategy_variant(original, "BOOLEAN_AND_COMMENT_HASH")

    assert varied["true_condition"].endswith("#")
    assert varied["false_condition"].endswith("#")
    first = experiment_strategy_manager.record(
        tool_name="sql_boolean_compare",
        stage="BOOLEAN_ORACLE",
        arguments={**original, "request": {"method": "POST", "url": "http://asset.local"}},
        independent_variable="asset_no",
        hypothesis="Boolean control",
    )
    second = experiment_strategy_manager.record(
        tool_name="sql_boolean_compare",
        stage="BOOLEAN_ORACLE",
        arguments={**varied, "request": {"method": "POST", "url": "http://asset.local"}},
        independent_variable="asset_no",
        hypothesis="Boolean control",
    )
    assert first["execution_fingerprint"] != second["execution_fingerprint"]
    assert first["strategy_fingerprint"] != second["strategy_fingerprint"]


def test_boolean_or_allowed_after_and_failed():
    diagnosis = analyze_boolean_oracle(_payload())
    result = ExperimentResultClassifier().classify(
        {"status": "COMPLETED"},
        diagnosis=diagnosis,
        strategy={
            "vulnerability_type": "SQL_INJECTION",
            "strategy_family": "BOOLEAN",
            "strategy_variant": "AND",
        },
        family_attempts=1,
        explicit_result="COMPLETED",
    )

    assert result["classification"] == "TRUE_SIDE_FAILED"
    assert "BOOLEAN_OR" in result["recommended_strategies"]


def test_no_signal_migrates_strategy():
    diagnosis = analyze_boolean_oracle(
        _payload(
            stable_true=False,
            stable_false=False,
            true_signature={},
            false_signature={},
        )
    )
    result = ExperimentResultClassifier().classify(
        {"status": "COMPLETED"},
        diagnosis=diagnosis,
        strategy={"strategy_family": "BOOLEAN", "strategy_variant": "AND"},
        family_attempts=1,
        explicit_result="COMPLETED",
    )

    assert result["classification"] == "NO_SIGNAL"
    assert result["recommended_strategies"][0] == "BOOLEAN_OR"

