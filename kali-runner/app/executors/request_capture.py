"""Capture a successful bounded request for SQLMap or manual replay."""
from __future__ import annotations

import json
import re
from urllib.parse import urlencode, urlsplit, urlunsplit

from fastapi import HTTPException

from app.models import JobRequest
from app.workspace.paths import safe_child, workspace_for


def _redact(value: object) -> object:
    if isinstance(value, dict):
        return {str(k): ("<secret-ref>" if "secret" in str(k).lower() or str(k).lower() in {"cookie", "authorization", "token"} else _redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


async def request_capture(request: JobRequest) -> dict:
    workspace = workspace_for(request.run_id)
    args = request.arguments
    source = args.get("request_file")
    if source:
        source_path = safe_child(workspace, str(source))
        relative = str(source_path.relative_to(workspace)).replace("\\", "/")
        if not source_path.is_file() or not relative.startswith("requests/"):
            raise HTTPException(422, detail="request_file must be an existing requests/** file")
        raw = source_path.read_text(encoding="utf-8", errors="replace")
        host_match = re.search(r"(?im)^Host:\s*([^\s:]+)", raw)
        if host_match and host_match.group(1).lower() not in {str(item).lower() for item in request.allowed_hosts}:
            raise HTTPException(403, detail="request_file host is outside the challenge allowlist")
        metadata = {"source_request_file": relative}
    else:
        method = str(args.get("method") or "GET").upper()
        url = str(args.get("url") or "")
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme not in {"http", "https"} or not host:
            raise HTTPException(422, detail="request_capture requires an absolute HTTP(S) URL")
        if host not in {str(item).lower() for item in request.allowed_hosts}:
            raise HTTPException(403, detail="request_capture host is outside the challenge allowlist")
        query = args.get("query") or {}
        query_text = urlencode(query, doseq=True) if isinstance(query, (dict, list, tuple)) else parts.query
        path = urlunsplit(("", "", parts.path or "/", query_text, ""))
        headers = {str(k): str(v) for k, v in (args.get("headers") or {}).items()}
        headers.setdefault("Host", host)
        cookie = args.get("cookie")
        cookie_secret_ref = args.get("cookie_secret_ref")
        if cookie_secret_ref:
            cookie = f"<secret-ref:{cookie_secret_ref}>"
        if cookie:
            headers["Cookie"] = str(cookie)
        body = args.get("body")
        if body is None and args.get("json") is not None:
            body = json.dumps(args.get("json"), ensure_ascii=False)
        if body is None and args.get("form") is not None:
            body = urlencode(args.get("form") or {}, doseq=True)
        body_text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False) if body is not None else ""
        if body_text:
            headers.setdefault("Content-Length", str(len(body_text.encode())))
        raw = f"{method} {path} HTTP/1.1\r\n" + "\r\n".join(f"{key}: {value}" for key, value in headers.items()) + "\r\n\r\n" + body_text
        metadata = {"method": method, "url": url, "host": host, "query": query, "headers": _redact(headers), "body": _redact(body), "body_type": "json" if args.get("json") is not None else "form" if args.get("form") is not None else "raw", "injection_field": args.get("test_field") or args.get("parameter"), "control_fields": _redact(args.get("control_fields") or {}), "control_field_values": _redact(args.get("control_fields") or {}), "cookie_secret_ref": cookie_secret_ref, "secret_refs": [*list(args.get("secret_refs") or []), *([str(cookie_secret_ref)] if cookie_secret_ref else [])]}
    out = workspace / "requests"
    out.mkdir(parents=True, exist_ok=True)
    req_path, meta_path = out / "exploit.req", out / "exploit.req.meta.json"
    req_path.write_text(raw, encoding="utf-8")
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "COMPLETED", "summary": "Captured successful request", "artifact_paths": ["requests/exploit.req", "requests/exploit.req.meta.json"], "artifact_path": "requests/exploit.req", "structured_result": {"request_file": "requests/exploit.req", "metadata_file": "requests/exploit.req.meta.json", "metadata": metadata}}
