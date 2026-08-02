from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.executors import mysql_metadata_discovery as metadata_module
from app.executors.mysql_metadata_discovery import _require_contract
from app.config import settings
from app.models import JobRequest
from app.service import JobService


def _args(profile: dict | None = None) -> dict:
    return {
        "dbms": "mysql",
        "test_field": "department",
        "baseline_value": "OPS",
        "control_fields": {},
        "oracle": {"json_field": "matched"},
        "target_expression": "DATABASE()",
        "expression_type": "METADATA_DISCOVERY",
        "supporting_evidence_ids": ["e"],
        "supporting_fact_ids": ["f"],
        "source_hypothesis_id": "h",
        "approved_analysis_review_id": "r",
        "assumption_status": "VERIFIED",
        "extraction_profile": profile,
    }


def test_metadata_requires_adaptive_profile() -> None:
    with pytest.raises(HTTPException) as error:
        _require_contract(_args())
    assert error.value.detail == "EXTRACTION_PROFILE_NOT_AVAILABLE"


def test_metadata_accepts_supported_profile() -> None:
    _require_contract(_args({"profile_id": "AEP-test", "extraction_strategy": "DIRECT_CHARACTER_ENUMERATION", "substring_function": "SUBSTRING", "allowed_charset": "ABC"}))


def test_metadata_result_contract_keeps_execution_and_contract_status_separate() -> None:
    no_fact = JobService._result_payload({
        "status": "NO_FACT",
        "stage": "version",
        "error_code": "MYSQL_METADATA_EMPTY_RESULT",
        "tool_execution_completed": True,
        "retryable": True,
        "facts": {},
        "extracted_facts": {},
    })
    assert no_fact["status"] == "COMPLETED"
    assert no_fact["result_status"] == "NO_FACT"
    assert no_fact["tool_execution_completed"] is True

    contract = JobService._result_payload({
        "status": "CONTRACT_ERROR",
        "error_code": "MYSQL_METADATA_CONTRACT_ERROR",
    })
    assert contract["status"] == "FAILED"
    assert contract["result_status"] == "CONTRACT_ERROR"


@pytest.mark.asyncio
async def test_metadata_oracle_without_signal_is_no_fact(tmp_path, monkeypatch) -> None:
    settings.workspace_root = tmp_path

    async def no_signal(_: JobRequest) -> dict:
        return {"status": "COMPLETED", "body": '{"matched":"unknown"}', "status_code": 200}

    monkeypatch.setattr(metadata_module, "execute_http", no_signal)
    arguments = _args({"profile_id": "AEP-test", "extraction_strategy": "DIRECT_CHARACTER_ENUMERATION", "substring_function": "SUBSTRING", "allowed_charset": "ABC"})
    arguments.update({
        "request": {"method": "POST", "url": "http://target.test/check", "json": {"department": "OPS"}},
        "stage": "version",
    })
    result = await metadata_module.mysql_metadata_discovery(JobRequest(
        run_id="no-fact", allowed_hosts=["target.test"], tool="mysql_metadata_discovery", arguments=arguments,
    ))
    assert result["status"] == "NO_FACT"
    assert result["error_code"] == "MYSQL_METADATA_EMPTY_RESULT"
    assert result["tool_execution_completed"] is True
    assert result["extracted_facts"] == {}
    assert result["diagnostic"]["reason"] == "ORACLE_RESPONSE_UNRECOGNIZED"
