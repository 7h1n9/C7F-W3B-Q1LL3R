from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import asyncmy
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import validate_database_driver

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

REPO_ROOT = Path(__file__).resolve().parents[2]
DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "mysql+asyncmy://ctf_agent:ctf_agent@127.0.0.1:3307/ctf_agent",
)


def test_asyncmy_import_available() -> None:
    validate_database_driver(DATABASE_URL)
    assert asyncmy.__file__


def test_async_mysql_engine_connects() -> None:
    async def verify() -> None:
        engine = create_async_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=3600)
        try:
            async with engine.connect() as connection:
                assert (await connection.execute(text("SELECT 1"))).scalar() == 1
        finally:
            await engine.dispose()

    asyncio.run(verify())


def test_mysql_alembic_upgrade_head() -> None:
    environment = os.environ.copy()
    environment["APP_DATABASE_URL"] = DATABASE_URL
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT / "backend",
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_no_sqlite_fallback_when_mysql_driver_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import config

    def missing_driver(name: str):
        if name == "asyncmy":
            error = ModuleNotFoundError("No module named 'asyncmy'")
            error.name = "asyncmy"
            raise error
        raise AssertionError(f"unexpected import: {name}")

    monkeypatch.setattr(config.importlib, "import_module", missing_driver)
    with pytest.raises(RuntimeError, match="DATABASE_ASYNC_DRIVER_MISSING"):
        validate_database_driver(DATABASE_URL)
