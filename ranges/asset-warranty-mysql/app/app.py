from __future__ import annotations

import logging
import os
import uuid
from contextlib import closing
from typing import Any

import pymysql
from flask import Flask, jsonify, render_template, request


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("asset-warranty")


def _db_config() -> dict[str, Any]:
    return {
        "host": os.environ.get("DB_HOST", "asset-warranty-db"),
        "port": int(os.environ.get("DB_PORT", "3306")),
        "user": os.environ.get("DB_USER", "warranty_app"),
        "password": os.environ.get("DB_PASSWORD", ""),
        "database": os.environ.get("DB_NAME", "asset_warranty"),
        "charset": os.environ.get("DB_CHARSET", "utf8mb4"),
        "autocommit": True,
        "cursorclass": pymysql.cursors.DictCursor,
        "connect_timeout": 3,
        "read_timeout": 5,
        "write_timeout": 5,
    }


def _connect():
    return pymysql.connect(**_db_config())


def _error_code(exc: BaseException) -> Any:
    args = getattr(exc, "args", ())
    return args[0] if args else None


def _safe_query_error(trace_id: str, exc: BaseException) -> None:
    # Deliberately omit exception text: injected values and extracted data must
    # never be written to the service log.
    logger.warning(
        "sql query rejected trace_id=%s exception_class=%s mysql_error_code=%s",
        trace_id,
        type(exc).__name__,
        _error_code(exc),
    )


def _warranty_lookup(asset_no: str, department: str, trace_id: str) -> bool:
    # asset_no is parameterized. department is the intentionally authorized
    # SQL injection point for this lab and is parsed by the real MySQL server.
    sql = """
    SELECT id, asset_no, department, device_name, warranty_until, status
    FROM warranty_records
    WHERE asset_no = %s
      AND department = '%s'
    LIMIT 1
    """ % ("%s", department)
    try:
        with closing(_connect()) as connection, connection.cursor() as cursor:
            cursor.execute(sql, (asset_no,))
            return cursor.fetchone() is not None
    except pymysql.MySQLError as exc:
        _safe_query_error(trace_id, exc)
        return False


app = Flask(__name__)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/health")
def health():
    try:
        with closing(_connect()) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT DATABASE() AS database_name, VERSION() AS version")
            row = cursor.fetchone()
        return jsonify(
            {
                "status": "ready",
                "database": "connected",
                "dbms": "mysql",
                "database_name": row["database_name"],
                "version": row["version"],
            }
        )
    except pymysql.MySQLError as exc:
        trace_id = str(uuid.uuid4())
        _safe_query_error(trace_id, exc)
        return jsonify({"status": "not_ready", "database": "unavailable", "dbms": "mysql"}), 503


@app.post("/api/warranty/check")
def warranty_check():
    payload = request.get_json(silent=True) or {}
    asset_no = str(payload.get("asset_no", ""))
    department = str(payload.get("department", ""))
    trace_id = str(uuid.uuid4())
    matched = _warranty_lookup(asset_no, department, trace_id)
    if matched:
        return jsonify({"matched": True, "message": "存在符合条件的保修记录"})
    return jsonify({"matched": False, "message": "查询条件无效" if department else "未找到符合条件的保修记录"})
