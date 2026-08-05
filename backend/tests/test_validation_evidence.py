from app.security.decision import security_decision_engine
from app.security.schemas import ValidationEvidenceStatus
from app.security.service import validation_evidence_service


def test_sql_boolean_differential_becomes_validated_result():
    result = validation_evidence_service.from_result("SQL_INJECTION", {
        "stable_true": True,
        "stable_false": True,
        "response_differential": True,
        "boolean_oracle_confirmed": True,
        "test_field": "asset_no",
        "request": {"method": "POST", "url": "http://target.test/check"},
    })
    assert result.vulnerability_type == "SQL_INJECTION"
    assert result.status == ValidationEvidenceStatus.VALIDATED


def test_other_vulnerability_types_are_supported_by_mock_evidence():
    cases = [
        ("XSS", {"payload_reflection": True}),
        ("SSRF", {"internal_response_evidence": True}),
        ("FILE_UPLOAD", {"upload_accepted": True, "retrievable": True}),
        ("COMMAND_INJECTION", {"command_executed": True}),
    ]
    for vulnerability_type, payload in cases:
        result = validation_evidence_service.from_result(vulnerability_type, payload)
        assert result.vulnerability_type == vulnerability_type
        assert result.status == ValidationEvidenceStatus.VALIDATED


def test_planner_routes_validation_statuses():
    assert security_decision_engine.decide({"validation_results": [{"status": "VALIDATED"}]}).required_phase == "EXPLOITATION"
    assert security_decision_engine.decide({"validation_results": [{"status": "INCONCLUSIVE"}]}).required_phase == "VALIDATION"
    assert security_decision_engine.decide({"validation_results": [{"status": "FAILED"}]}).required_phase == "VALIDATION"
