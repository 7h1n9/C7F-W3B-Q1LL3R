from __future__ import annotations

import asyncio
import json

from app.executors import oracle_expression_calibration as calibration_module
from app.models import JobRequest


def _request(matrix: list[dict]) -> JobRequest:
    return JobRequest(
        run_id="adaptive-profile-test",
        allowed_hosts=["warranty.test"],
        tool="oracle_expression_calibration",
        arguments={
            "request": {"url": "http://warranty.test/check", "method": "POST", "json": {"department": "OPS"}},
            "test_field": "department",
            "baseline_value": "OPS",
            "oracle": {"json_field": "matched", "true_value": True, "false_value": False},
            "predicate_template": "' AND ({predicate}) -- ",
            "matrix": matrix,
            "max_calibration_requests": 80,
        },
    )


def _fake_http(mode: str):
    async def execute(request: JobRequest) -> dict:
        payload = json.dumps(request.arguments.get("json") or {})
        if mode == "direct":
            matched = "SUBSTRING('ABC',1,1)='A'" in payload or "STRCMP('A','A')=0" in payload
        elif mode == "hex":
            matched = "HEX('A')='41'" in payload or "HEX(SUBSTRING('ABC',1,1))='41'" in payload
            matched = matched or "SUBSTRING('ABC',1,1)='A'" in payload
            matched = matched or "CONV(HEX('A'),16,10)=65" in payload
        elif mode == "like":
            matched = "LIKE 'A%'" in payload or "DATABASE() LIKE '%'" in payload
        else:
            matched = False
        if "ASCII" in payload or "ascii(" in payload or "ORD(" in payload or "ord(" in payload:
            matched = False
        return {"status_code": 200, "body": json.dumps({"matched": matched, "message": "ok"}), "body_length": 30}

    return execute


def test_ascii_failure_does_not_fail_level_two(monkeypatch) -> None:
    monkeypatch.setattr(calibration_module, "execute_http", _fake_http("direct"))
    result = asyncio.run(calibration_module.oracle_expression_calibration(_request([
        {"level": 2, "name": "ascii", "primitive": "ascii", "true": "ASCII('A')=65", "false": "ASCII('A')=66"},
        {"level": 2, "name": "substring", "primitive": "substring", "function": "SUBSTRING", "true": "SUBSTRING('ABC',1,1)='A'", "false": "SUBSTRING('ABC',1,1)='B'"},
    ])))
    structured = result["structured_result"]
    assert structured["status"] == "COMPLETED"
    assert structured["ascii_failure_classification"] == "ASCII_TOKEN_FILTERED"
    assert structured["adaptive_extraction_profile"]["extraction_strategy"] == "DIRECT_CHARACTER_ENUMERATION"
    assert structured["capabilities"]["character_extraction_oracle_confirmed"] is True


def test_hex_can_replace_ascii(monkeypatch) -> None:
    monkeypatch.setattr(calibration_module, "execute_http", _fake_http("hex"))
    result = asyncio.run(calibration_module.oracle_expression_calibration(_request([
        {"level": 2, "name": "ascii", "primitive": "ascii", "true": "ASCII('A')=65", "false": "ASCII('A')=66"},
        {"level": 2, "name": "ord", "primitive": "ord", "true": "ORD('A')=65", "false": "ORD('A')=66"},
        {"level": 2, "name": "hex", "primitive": "hex", "function": "HEX", "true": "HEX('A')='41'", "false": "HEX('A')='42'"},
        {"level": 2, "name": "conv_hex", "primitive": "conv", "function": "CONV", "true": "CONV(HEX('A'),16,10)=65", "false": "CONV(HEX('A'),16,10)=66"},
        {"level": 2, "name": "substring", "primitive": "substring", "function": "SUBSTRING", "true": "SUBSTRING('ABC',1,1)='A'", "false": "SUBSTRING('ABC',1,1)='B'"},
    ])))
    profile = result["structured_result"]["adaptive_extraction_profile"]
    assert profile["extraction_strategy"] == "HEX_BINARY_SEARCH"
    assert profile["ascii_supported"] is False
    assert profile["hex_supported"] is True


def test_like_prefix_profile(monkeypatch) -> None:
    monkeypatch.setattr(calibration_module, "execute_http", _fake_http("like"))
    result = asyncio.run(calibration_module.oracle_expression_calibration(_request([
        {"level": 2, "name": "like", "primitive": "like", "true": "'ABC' LIKE 'A%'", "false": "'ABC' LIKE 'B%'"},
    ])))
    structured = result["structured_result"]
    assert structured["adaptive_extraction_profile"]["extraction_strategy"] == "PREFIX_LIKE_ENUMERATION"
    assert structured["capabilities"]["bounded_character_enumeration_supported"] is True


def test_all_character_primitives_pause_without_profile(monkeypatch) -> None:
    monkeypatch.setattr(calibration_module, "execute_http", _fake_http("none"))
    result = asyncio.run(calibration_module.oracle_expression_calibration(_request([
        {"level": 2, "name": "ascii", "primitive": "ascii", "true": "ASCII('A')=65", "false": "ASCII('A')=66"},
        {"level": 2, "name": "substring", "primitive": "substring", "true": "SUBSTRING('ABC',1,1)='A'", "false": "SUBSTRING('ABC',1,1)='B'"},
    ])))
    structured = result["structured_result"]
    assert structured["adaptive_extraction_profile"] is None
    assert structured["error_code"] == "NO_CHARACTER_EXTRACTION_PRIMITIVE"
    assert result["status"] == "COMPLETED"
