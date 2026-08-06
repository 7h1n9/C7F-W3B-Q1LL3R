from types import SimpleNamespace

from app.services.security_fact_reducer import security_fact_reducer
from app.services.tool_result_fact_reducer import tool_result_fact_reducer


def _challenge(case_id="sql-injection-golden", **metadata):
    return SimpleNamespace(metadata_json={"benchmark_case_id": case_id, **metadata})


def _call(call_id, arguments=None, tool_name="http_request"):
    return SimpleNamespace(id=call_id, tool_name=tool_name, arguments_json=arguments or {})


def _observation(body):
    return SimpleNamespace(facts_json={"tool_model_view": {"content_excerpt": body}})


def _payload(body):
    return {"structured_result": {"status_code": 200, "body": body}}


def test_sql_injection_validation_requires_true_false_pair_and_differential():
    true_call = _call("true-call")
    false_call = _call("false-call")
    candidates = security_fact_reducer.reduce(
        _challenge(),
        true_call,
        _observation('{"matched":true,"oracle":"TRUE"}'),
        _payload('{"matched":true,"oracle":"TRUE"}'),
        ["e-true"],
        paired_records=[
            (false_call, _observation('{"matched":false,"oracle":"FALSE"}'), _payload('{"matched":false,"oracle":"FALSE"}')),
        ],
    )

    assert any(item["fact_type"] == "SECURITY_VALIDATION" for item in candidates)
    assert any(item["fact_key"] == "security.sql_injection.validation" and item["value"]["status"] == "VALIDATED" for item in candidates)


def test_sql_injection_exploit_is_reduced_from_disclosure_response():
    candidates = security_fact_reducer.reduce(
        _challenge(),
        _call("exploit-call"),
        _observation('{"extracted_data":["FLAG{GOLDEN_PATH_SQL_INJECTION}"],"disclosure":"database_data_disclosure"}'),
        _payload('{"extracted_data":["FLAG{GOLDEN_PATH_SQL_INJECTION}"],"disclosure":"database_data_disclosure"}'),
        ["e-exploit"],
    )

    assert len(candidates) == 1
    assert candidates[0]["fact_type"] == "SECURITY_EXPLOIT"
    assert candidates[0]["value"]["status"] == "SUCCESS"


def test_ordinary_http_response_does_not_create_security_fact():
    candidates = security_fact_reducer.reduce(
        _challenge(),
        _call("ordinary-call"),
        _observation('{"message":"hello"}'),
        _payload('{"message":"hello"}'),
        ["e-ordinary"],
    )

    assert candidates == []


def test_asset_warranty_http_baseline_regression():
    challenge = _challenge(case_id="not-a-benchmark", adapter="asset_warranty")
    valid_candidates = tool_result_fact_reducer._reduce_one(
        challenge,
        _call("baseline-call"),
        _observation('{"matched":true}'),
        _payload('{"matched":true}'),
        ["e-baseline"],
    )
    invalid_candidates = tool_result_fact_reducer._reduce_one(
        challenge,
        _call("invalid-call"),
        _observation('{"matched":false}'),
        _payload('{"matched":false}'),
        ["e-invalid"],
    )

    assert any(item["fact_key"] == "asset_warranty.valid_baseline" for item in valid_candidates)
    assert any(item["fact_key"] == "asset_warranty.invalid_baseline" for item in invalid_candidates)
