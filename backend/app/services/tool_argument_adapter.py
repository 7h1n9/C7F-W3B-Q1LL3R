"""Compatibility adapter for model/provider argument naming drift."""
ALIASES = {"target_url":"url", "target":"url", "session":"session_name", "session_id":"session_name", "data":"body", "form_data":"form", "params":"query", "cookie":"headers.Cookie", "artifact":"path", "artifact_path":"path"}

def adapt_arguments(tool: str, arguments: dict) -> dict:
    out = dict(arguments or {})
    for old, new in ALIASES.items():
        if old in out and new not in out:
            value = out.pop(old)
            if new == "headers.Cookie":
                headers = dict(out.get("headers") or {}); headers["Cookie"] = value; out["headers"] = headers
            else: out[new] = value
    if tool == "http_extract" and "url" in out and "path" not in out:
        out["url"] = out["url"]
    return out
