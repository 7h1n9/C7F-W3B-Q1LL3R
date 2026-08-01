from __future__ import annotations

import asyncio

from app.main import capability_registry
from app.tool_registry import registration


def _tool(payload: dict, name: str) -> dict:
    return next(item for item in payload["tools"] if item["name"] == name)


def test_mysql_metadata_executor_registered() -> None:
    item = registration("mysql_metadata_discovery")
    assert item.implemented is True
    assert item.supported_dbms == ("mysql",)


def test_mysql_metadata_capability_true() -> None:
    payload = asyncio.run(capability_registry())
    item = _tool(payload, "mysql_metadata_discovery")
    assert payload["mysql_metadata_discovery"] is True
    assert "mysql" in payload["supported_dbms"]
    assert item["implemented"] is True
    assert item["installed"] is True
    assert item["enabled"] is True
    assert item["self_test_ok"] is True


def test_boolean_extractor_is_mysql_capable() -> None:
    item = registration("boolean_config_extract")
    assert "mysql" in item.supported_dbms


def test_oracle_calibration_executor_registered() -> None:
    item = registration("oracle_expression_calibration")
    assert item.implemented is True
    assert item.supported_dbms == ("mysql",)
