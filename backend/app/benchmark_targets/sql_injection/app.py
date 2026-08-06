"""Intentionally easy SQL Injection target for local benchmark runs.

This target is deterministic by design. It models the vulnerable application's
observable contract without executing attacker-controlled SQL, so it is safe to
run in tests while still exposing a clear TRUE/FALSE oracle and a bounded data
disclosure path.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Query

from app.benchmark_targets.sql_injection.database import all_secrets, connect, find_asset, seed


def _is_true_condition(value: str) -> bool:
    normalized = value.lower().replace(" ", "")
    return "'and1=1--" in normalized or "'or1=1--" in normalized


def _is_false_condition(value: str) -> bool:
    normalized = value.lower().replace(" ", "")
    return "'and1=2--" in normalized or "'or1=2--" in normalized


def _is_secret_extraction(value: str) -> bool:
    normalized = value.lower()
    return "union" in normalized and "select" in normalized and "secret" in normalized


def create_app(db_path: str | Path | None = None) -> FastAPI:
    connection = connect(db_path)
    try:
        seed(connection)
    finally:
        connection.close()

    application = FastAPI(title="SQL Injection Easy Benchmark", version="1.0")

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "benchmark": "sql_injection_easy"}

    @application.get("/search")
    async def search(asset_no: str = Query(default="")) -> dict:
        if _is_secret_extraction(asset_no):
            connection = connect(db_path)
            try:
                return {
                    "matched": True,
                    "extracted_data": all_secrets(connection),
                    "disclosure": "database_data_disclosure",
                }
            finally:
                connection.close()

        if _is_true_condition(asset_no):
            return {"matched": True, "oracle": "TRUE", "control": "boolean"}
        if _is_false_condition(asset_no):
            return {"matched": False, "oracle": "FALSE", "control": "boolean"}

        connection = connect(db_path)
        try:
            return {"matched": find_asset(connection, asset_no) is not None}
        finally:
            connection.close()

    return application


app = create_app()
