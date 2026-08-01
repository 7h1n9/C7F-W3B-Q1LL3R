from __future__ import annotations

import asyncio
import json

from app.executors import oracle_expression_calibration as calibration_module
from app.models import JobRequest


def _request(matrix: list[dict]) -> JobRequest:
    return JobRequest(
        run_id="calibration-test",
        allowed_hosts=["warranty.test"],
        tool="oracle_expression_calibration",
        arguments={
            "request": {"url": "http://warranty.test/check", "method": "POST", "json": {"department": "OPS"}},
            "test_field": "department",
            "baseline_value": "OPS",
            "control_fields": {},
            "oracle": {"json_field": "matched", "true_value": True, "false_value": False},
            "predicate_template": "' AND ({predicate}) -- ",
            "matrix": matrix,
            "max_calibration_requests": 8,
        },
    )


def test_calibration_requires_paired_true_false_and_repeats(monkeypatch) -> None:
    async def fake_http(request: JobRequest) -> dict:
        body = json.dumps(request.arguments.get("json") or {})
        matched = "1=1" in body or "(1+1)=2" in body
        return {"status_code": 200, "body": json.dumps({"matched": matched, "message": "ok"}), "body_length": 30}

    monkeypatch.setattr(calibration_module, "execute_http", fake_http)
    result = asyncio.run(calibration_module.oracle_expression_calibration(_request([
        {"level": 0, "name": "literal", "true": "1=1", "false": "1=2", "capability": "boolean_predicate_oracle_confirmed"},
        {"level": 1, "name": "arithmetic", "true": "(1+1)=2", "false": "(1+1)=3", "capability": "expression_oracle_confirmed"},
    ])))
    assert result["status"] == "COMPLETED"
    assert result["structured_result"]["status"] == "PARTIAL"
    assert all(item["passed"] for item in result["structured_result"]["calibration_matrix"])
    assert result["structured_result"]["requests"] == 8


def test_calibration_distinguishes_no_signal_from_false(monkeypatch) -> None:
    async def fake_http(request: JobRequest) -> dict:
        return {"status_code": 200, "body": json.dumps({"matched": False, "message": "same"}), "body_length": 31}

    monkeypatch.setattr(calibration_module, "execute_http", fake_http)
    result = asyncio.run(calibration_module.oracle_expression_calibration(_request([
        {"level": 4, "name": "mysql_version", "true": "VERSION() IS NOT NULL", "false": "VERSION() IS NULL", "capability": "mysql_dbms_confirmed"},
    ])))
    assert result["status"] == "COMPLETED"
    assert result["structured_result"]["status"] == "NO_SIGNAL"
    assert result["structured_result"]["error_code"] == "NO_DISCRIMINATING_SIGNAL"
