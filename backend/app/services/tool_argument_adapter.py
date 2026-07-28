"""Compatibility adapter for model/provider argument naming drift.

The adapter is intentionally challenge-agnostic.  Target URLs, request
payloads, fields and expressions must come from the run's evidence or
checkpoint, never from a hard-coded challenge default.
"""

ALIASES = {
    "target_url": "url",
    "target": "url",
    "session": "session_name",
    "session_id": "session_name",
    "data": "body",
    "form_data": "form",
    "params": "query",
    "cookie": "headers.Cookie",
    "artifact": "path",
    "artifact_path": "path",
}
_REQUEST_TOOLS = {
    "sql_injection_probe",
    "sql_boolean_compare",
    "sql_union_probe",
    "oracle_probe_matrix",
    "boolean_config_extract",
}


def adapt_arguments(tool: str, arguments: dict, challenge: object | None = None) -> dict:
    """Normalize aliases while preserving caller-provided challenge data."""
    del challenge  # retained in the public signature for existing callers
    out = dict(arguments or {})
    for old, new in ALIASES.items():
        if old in out and new not in out:
            value = out.pop(old)
            if new == "headers.Cookie":
                headers = dict(out.get("headers") or {})
                headers["Cookie"] = value
                out["headers"] = headers
            else:
                out[new] = value
    if tool == "http_extract" and "url" in out and "path" not in out:
        out["url"] = out["url"]
    if tool not in _REQUEST_TOOLS:
        return out

    request = out.get("request") if isinstance(out.get("request"), dict) else {}
    # Old providers sent RequestSpec fields at the top level.  Move only the
    # structure; do not invent method, URL, body, field, SQL, or oracle data.
    for field in ("method", "url", "headers", "json", "form"):
        if field in out:
            request.setdefault(field, out.pop(field))
    out["request"] = request
    if "parameter" in out and "test_field" not in out:
        out["test_field"] = out.pop("parameter")
    oracle = out.get("oracle")
    if isinstance(oracle, dict) and "field" in oracle and "json_field" not in oracle:
        oracle = dict(oracle)
        oracle["json_field"] = oracle.pop("field")
        out["oracle"] = oracle
    return out
