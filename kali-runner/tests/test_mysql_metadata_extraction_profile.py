from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.executors.mysql_metadata_discovery import _require_contract


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
