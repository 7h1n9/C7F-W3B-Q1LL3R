from types import SimpleNamespace

from app.models.challenge import Challenge
from app.services.tool_result_fact_reducer import tool_result_fact_reducer
from app.tools.gateway import ToolGateway


def _arguments():
    return {
        "test_field": "asset_no",
        "baseline_value": "PC-2026-013",
        "request": {"method": "POST", "url": "http://target.test/check", "json": {"department": "OPS"}},
        "control_fields": {"department": "OPS"},
        "oracle": {"json_field": "matched", "true_value": True, "false_value": False},
    }


def _structured(*, confirmed=True, stable_true=True, stable_false=True, differential=True):
    return {
        "status": "COMPLETED",
        "boolean_oracle_confirmed": confirmed,
        "stable_true": stable_true,
        "stable_false": stable_false,
        "true_false_differential": differential,
        "true_results": [{"signature": {"status_code": 200, "body_length": 17, "matched": True}}],
        "false_results": [{"signature": {"status_code": 200, "body_length": 18, "matched": False}}],
    }


def _rows(structured):
    challenge = Challenge(metadata_json={"adapter": "asset_warranty", "dbms": "mysql"})
    call = SimpleNamespace(tool_name="sql_boolean_compare", arguments_json=_arguments(), id="call-1", run_id="run-1")
    observation = SimpleNamespace(facts_json={"tool_model_view": {"extracted_facts": structured}})
    return challenge, call, observation


def test_boolean_oracle_valid_result_creates_candidate_fact():
    challenge, call, observation = _rows(_structured())
    candidates = tool_result_fact_reducer._reduce_one(challenge, call, observation, {}, ["E-1"])

    assert len(candidates) == 1
    assert candidates[0]["fact_key"] == "asset_warranty.mysql_boolean_oracle"
    assert candidates[0]["fact_type"] == "BOOLEAN_ORACLE"
    assert candidates[0]["value"]["validation_result"]["status"] == "VALIDATED"
    assert candidates[0]["value"]["validation_result"]["vulnerability_type"] == "SQL_INJECTION"


def test_boolean_oracle_unstable_result_creates_no_candidate():
    challenge, call, observation = _rows(_structured(confirmed=False, stable_true=False, differential=False))
    candidates = tool_result_fact_reducer._reduce_one(challenge, call, observation, {}, ["E-1"])

    assert candidates == []


def test_generic_boolean_artifact_survives_empty_observation_placeholders():
    challenge = Challenge(metadata_json={"benchmark_case_id": "sql-injection-golden"})
    call = SimpleNamespace(
        tool_name="sql_boolean_compare",
        arguments_json={**_arguments(), "request": {"method": "GET", "url": "http://target.test/search", "query": {"asset_no": "PC-001"}}, "test_field": "asset_no", "baseline_value": "PC-001"},
        id="generic-call",
        run_id="run-1",
    )
    observation = SimpleNamespace(facts_json={
        "tool_model_view": {"extracted_facts": {"response_differential": None}},
        "stable_true": None,
        "stable_false": None,
        "true_results": [],
        "false_results": [],
        "boolean_oracle_confirmed": None,
    })
    artifact = _structured()
    candidates = tool_result_fact_reducer._reduce_one(
        challenge,
        call,
        observation,
        {"structured_result": artifact},
        ["E-GENERIC"],
    )

    assert len(candidates) == 1
    assert candidates[0]["fact_key"] == "security.sql_injection.validation"
    assert candidates[0]["value"]["status"] == "VALIDATED"
    assert candidates[0]["value"]["confidence"] == 0.95
    assert candidates[0]["value"]["controls"] == {
        "baseline": True,
        "positive_control": True,
        "negative_control": True,
    }


def test_gateway_observation_preserves_boolean_contract_fields():
    structured = _structured()
    facts = ToolGateway._facts("sql_boolean_compare", {"structured_result": structured}, "oracle.json")

    assert facts["stable_true"] is True
    assert facts["stable_false"] is True
    assert facts["true_false_differential"] is True
    assert facts["true_signature"]["matched"] is True
    assert facts["false_signature"]["matched"] is False
    assert facts["response_differential"] is True
    assert facts["boolean_oracle_confirmed"] is True
    assert facts["request_contract"] == {}
