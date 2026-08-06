"""Small SQLite backing store for the deterministic benchmark target."""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parents[4] / "data" / "benchmark_targets" / "sql_injection_easy.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY,
    asset_no TEXT NOT NULL UNIQUE,
    owner TEXT NOT NULL,
    secret TEXT NOT NULL
)
"""

SEED_ASSETS = (
    (1, "PC-001", "Alice", "FLAG{GOLDEN_PATH_SQL_INJECTION}"),
    (2, "PC-002", "Bob", "asset-secret-002"),
)


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    database_path = Path(path) if path is not None else DEFAULT_DB_PATH
    if str(database_path) != ":memory:":
        database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(database_path))
    connection.row_factory = sqlite3.Row
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.execute(SCHEMA)
    connection.commit()


def seed(connection: sqlite3.Connection) -> None:
    initialize(connection)
    connection.executemany(
        "INSERT OR REPLACE INTO assets (id, asset_no, owner, secret) VALUES (?, ?, ?, ?)",
        SEED_ASSETS,
    )
    connection.commit()


def find_asset(connection: sqlite3.Connection, asset_no: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT id, asset_no, owner, secret FROM assets WHERE asset_no = ?",
        (asset_no,),
    ).fetchone()


def all_secrets(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute("SELECT secret FROM assets ORDER BY id").fetchall()
    return [str(row["secret"]) for row in rows]
