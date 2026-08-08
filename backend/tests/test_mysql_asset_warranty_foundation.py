"""MySQL-only foundation acceptance tests for the asset-warranty route."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.challenge_adapters.asset_warranty import AssetWarrantyAdapter
from app.services.skill_selection import allowed_tools_for
from app.tools.registry import load_tool_definitions

REPO_ROOT = Path(__file__).resolve().parents[2]
pytestmark = [pytest.mark.integration, pytest.mark.mysql]
MYSQL_ADMIN_URL = os.getenv(
    "MYSQL_ADMIN_URL",
    "mysql+asyncmy://root:change-me-before-use@127.0.0.1:3307/mysql",
)


def _database_url(database: str) -> str:
    return make_url(MYSQL_ADMIN_URL).set(database=database).render_as_string(hide_password=False)


def _run_alembic(database_url: str, revision: str = "head") -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["APP_DATABASE_URL"] = database_url
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=REPO_ROOT / "backend",
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _create_database(database: str) -> None:
    async def create() -> None:
        engine = create_async_engine(MYSQL_ADMIN_URL)
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
        finally:
            await engine.dispose()

    asyncio.run(create())


def _drop_database(database: str) -> None:
    async def drop() -> None:
        engine = create_async_engine(MYSQL_ADMIN_URL)
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f"DROP DATABASE IF EXISTS `{database}`"))
        finally:
            await engine.dispose()

    asyncio.run(drop())


def _assert_mysql_available() -> None:
    try:
        _create_database("ctf_foundation_probe")
    except Exception as error:  # pragma: no cover - environment-specific guard
        pytest.fail(f"MySQL acceptance database is unavailable: {error}")
    finally:
        _drop_database("ctf_foundation_probe")


def test_asset_warranty_routes_to_mysql() -> None:
    metadata = {
        "adapter": "asset_warranty",
        "dbms": "mysql",
        "endpoint": "/api/warranty/check",
        "method": "POST",
        "content_type": "application/json",
        "fields": ["asset_no", "department"],
        "control_values": {"asset_no": "PC-2026-013", "department": "OPS"},
    }
    from types import SimpleNamespace

    challenge = SimpleNamespace(metadata_json=metadata, target_url="http://warranty.test", allowed_hosts=["warranty.test"])
    assert AssetWarrantyAdapter.matches(challenge)
    assert "mysql_metadata" in AssetWarrantyAdapter.context(challenge)["stages"]


def test_asset_warranty_never_routes_to_sqlite() -> None:
    metadata = {"adapter": "asset_warranty", "dbms": "mysql"}
    tools = allowed_tools_for("WEB_TARGET", metadata)
    assert "mysql_metadata_discovery" in tools
    assert "sqlite_metadata_discovery" not in tools
    assert not AssetWarrantyAdapter.matches(type("Challenge", (), {"metadata_json": {**metadata, "dbms": "sqlite"}})())


def test_mysql_metadata_tool_registered() -> None:
    definition = load_tool_definitions()["mysql_metadata_discovery"]
    assert definition.enabled
    assert {"request", "test_field", "oracle", "target_expression"} <= set(definition.parameters)


def test_mysql_migration_fresh_database() -> None:
    _assert_mysql_available()
    database = f"ctf_fresh_{uuid.uuid4().hex[:12]}"
    _create_database(database)
    try:
        result = _run_alembic(_database_url(database))
        assert result.returncode == 0, result.stdout + result.stderr
        async def verify() -> None:
            engine = create_async_engine(_database_url(database))
            try:
                async with engine.connect() as connection:
                    assert (await connection.execute(text("SELECT VERSION()"))).scalar()
                    assert (await connection.execute(text("SELECT DATABASE()"))).scalar() == database
                    assert (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalar() == "0041_attack_strategy_memory"
            finally:
                await engine.dispose()

        asyncio.run(verify())
    finally:
        _drop_database(database)


def test_mysql_migration_existing_database() -> None:
    _assert_mysql_available()
    database = f"ctf_existing_{uuid.uuid4().hex[:12]}"
    _create_database(database)
    try:
        database_url = _database_url(database)
        existing = _run_alembic(database_url, "0035_planner_reference_types")
        assert existing.returncode == 0, existing.stdout + existing.stderr
        upgraded = _run_alembic(database_url)
        assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    finally:
        _drop_database(database)
