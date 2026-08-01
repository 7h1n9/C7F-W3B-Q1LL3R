from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import tool_manifest
from app.tools.registry import load_tool_definitions


def test_mysql_metadata_tool_definition_valid() -> None:
    definition = load_tool_definitions()["mysql_metadata_discovery"]
    assert definition.enabled is True
    assert {"dbms", "discovery_scope", "request", "test_field", "oracle", "target_expression", "max_tables", "max_columns", "max_name_length", "max_requests", "resume"} <= set(definition.parameters)
    arguments = {
        "dbms": "mysql", "discovery_scope": "current_database", "request": {},
        "test_field": "department", "baseline_value": "OPS", "control_fields": {},
        "oracle": {}, "target_expression": "DATABASE()", "expression_type": "METADATA_DISCOVERY",
        "supporting_evidence_ids": ["e"], "supporting_fact_ids": ["f"],
        "source_hypothesis_id": "h", "approved_analysis_review_id": "r",
        "assumption_status": "VERIFIED", "max_tables": 10, "max_columns": 30,
        "max_name_length": 128, "max_requests": 2000, "resume": True,
    }
    assert definition.validate_arguments(arguments) == arguments


def test_oracle_calibration_tool_definition_valid() -> None:
    definition = load_tool_definitions()["oracle_expression_calibration"]
    assert definition.enabled is True
    assert {"request", "test_field", "baseline_value", "oracle", "predicate_template", "matrix", "supporting_evidence_ids", "supporting_fact_ids"} <= set(definition.parameters)


def test_asset_warranty_excludes_sqlite_tools() -> None:
    from app.services.skill_selection import allowed_tools_for

    tools = allowed_tools_for("WEB_TARGET", {"adapter": "asset_warranty", "dbms": "mysql"})
    assert "mysql_metadata_discovery" in tools
    assert "sqlite_metadata_discovery" not in tools


@pytest.mark.asyncio
async def test_backend_manifest_accepts_runner_mysql_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "tools": [
            {"name": name, "implemented": True, "installed": True, "enabled": True, "self_test_ok": True}
            for name in ("http_request", "sql_boolean_compare", "mysql_metadata_discovery")
        ],
        "supported_dbms": ["mysql"],
        "network_enforcement": {},
    }
    monkeypatch.setattr(tool_manifest.runner_client, "health", AsyncMock(return_value={"build": {}}))
    monkeypatch.setattr(tool_manifest.runner_client, "capabilities", AsyncMock(return_value=payload))

    class FakeSession:
        scalar = AsyncMock(return_value=None)
        flush = AsyncMock()

        def add(self, item) -> None:
            self.item = item

    session = FakeSession()
    run = SimpleNamespace(
        id="run-1",
        role_snapshot_json={"tools": ["http_request", "sql_boolean_compare", "mysql_metadata_discovery"]},
        engine_type="codex_sdk",
    )
    attempt = SimpleNamespace(id="attempt-1", runtime_build_manifest_json={})
    challenge = SimpleNamespace(
        id="challenge-1", challenge_type="WEB_TARGET",
        metadata_json={"adapter": "asset_warranty", "dbms": "mysql"},
    )

    manifest = await tool_manifest.refresh_runtime_tool_manifest(session, run, attempt, challenge)

    assert "mysql_metadata_discovery" in manifest.backend_registry_tools
    assert "mysql_metadata_discovery" in manifest.runner_capability_tools
    assert "mysql_metadata_discovery" in manifest.effective_tools
    assert "sqlite_metadata_discovery" not in manifest.effective_tools
    assert manifest.execution_mode == "controller_tool_loop"
    assert manifest.mcp_required is False
