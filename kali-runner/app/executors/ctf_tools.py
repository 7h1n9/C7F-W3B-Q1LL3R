import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

from app.config import settings
from app.executors.http_executor import _extract_body
from app.executors.session_store import session_store
from app.models import JobRequest
from app.workspace.paths import safe_child, workspace_for


def _target(request: JobRequest, url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.hostname.lower() not in request.allowed_hosts:
        raise HTTPException(403, detail="target host is not allowlisted")


async def http_extract(request: JobRequest) -> dict:
    args = {**request.arguments, "follow_redirects": False}
    url = str(args.get("url", ""))
    _target(request, url)
    async with httpx.AsyncClient(timeout=settings.job_timeout_seconds, trust_env=False) as client:
        response = await client.request(str(args.get("method", "GET")).upper(), url, headers=args.get("headers", {}), params=args.get("query", {}), content=args.get("body"))
    body = response.content[: settings.http_excerpt_bytes].decode(errors="replace")
    selected_headers = {
        key: value[:500]
        for key, value in response.headers.items()
        if key.lower() in {"content-type", "location", "server", "x-powered-by", "www-authenticate"}
    }
    return {
        "status_code": response.status_code,
        "final_url": str(response.url),
        "content_type": response.headers.get("content-type", ""),
        "selected_headers": selected_headers,
        "body_excerpt": body,
        "extracted_facts": _extract_body(body, response.headers.get("content-type", "")),
        "summary": "HTTP response extracted",
    }


async def whatweb_fingerprint(request: JobRequest) -> dict:
    result = await http_extract(request)
    result["extracted_facts"]["technology_stack"] = [
        value
        for key, value in (result.get("selected_headers", {}) or {}).items()
        if key.lower() in {"server", "x-powered-by"}
    ]
    result["summary"] = "Web technology fingerprint extracted"
    return result


async def js_asset_analyze(request: JobRequest) -> dict:
    result = await http_extract(request)
    text = result.get("body_excerpt", "")
    urls = re.findall(r"(?:/|https?://)[^\"'\s]+", text)[:100]
    result["extracted_facts"] = {**result.get("extracted_facts", {}), "source_code_sinks": [item for item in ("fetch(", "XMLHttpRequest", "document.cookie", "localStorage", "eval(") if item in text], "referenced_urls": urls}
    result["summary"] = "JavaScript asset indicators extracted"
    return result


def _workspace_text(request: JobRequest) -> tuple[Path, str]:
    workspace = workspace_for(request.run_id)
    path = safe_child(workspace, str(request.arguments.get("path", "")))
    if not path.is_file():
        raise HTTPException(404, detail="file not found")
    text = path.read_text(encoding="utf-8", errors="replace")
    return path, text[: settings.max_output_bytes]


async def file_type(request: JobRequest) -> dict:
    path, text = _workspace_text(request)
    return {"summary": f"File type: {path.suffix or 'unknown'}", "extracted_facts": {"path": str(path), "suffix": path.suffix, "size": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}, "content_excerpt": text[:1000]}


async def strings_extract(request: JobRequest) -> dict:
    path, text = _workspace_text(request)
    strings = [line for line in text.splitlines() if len(line.strip()) >= 4][:500]
    return {"summary": f"Extracted {len(strings)} strings", "output": "\n".join(strings), "extracted_facts": {"path": str(path), "string_count": len(strings)}}


async def archive_list(request: JobRequest) -> dict:
    path, _ = _workspace_text(request)
    if not shutil.which("tar"):
        return {"summary": "Archive listing unavailable", "error_code": "ARCHIVE_TOOL_UNAVAILABLE", "status": "FAILED"}
    completed = subprocess.run(["tar", "-tf", str(path)], capture_output=True, text=True, timeout=10, check=False)
    return {"summary": "Archive entries listed", "output": completed.stdout[: settings.max_output_bytes], "extracted_facts": {"path": str(path), "exit_code": completed.returncode}}


async def source_map_analyze(request: JobRequest) -> dict:
    path, text = _workspace_text(request)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        return {"summary": "Source map JSON is invalid", "status": "FAILED", "error_code": "SOURCE_MAP_INVALID", "error": str(error)}
    return {"summary": "Source map metadata extracted", "extracted_facts": {"path": str(path), "version": value.get("version"), "sources": value.get("sources", [])[:100], "names": value.get("names", [])[:100], "file": value.get("file")}}


async def content_discovery(request: JobRequest) -> dict:
    base = str(request.arguments.get("url", "")).rstrip("/")
    _target(request, base)
    words = request.arguments.get("words") or ["admin", "login", "robots.txt", ".git", "backup", "config"]
    words = [str(word).strip().lstrip("/") for word in list(words)[:50] if str(word).strip()]
    hits = []
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        for word in words:
            response = await client.get(f"{base}/{word}")
            if response.status_code not in {404, 410}:
                hits.append({"path": word, "status_code": response.status_code, "content_length": len(response.content)})
    return {"summary": f"Content discovery found {len(hits)} candidates", "extracted_facts": {"hits": hits, "word_count": len(words)}}


async def jwt_inspect(request: JobRequest) -> dict:
    try:
        token = session_store.get_secret(request.run_id, str(request.arguments["token_ref"])) if request.arguments.get("token_ref") else str(request.arguments.get("token", ""))
    except KeyError:
        return {"summary": "JWT secret reference not found", "status": "FAILED", "error_code": "SECRET_REF_NOT_FOUND"}
    parts = token.split(".")
    if len(parts) != 3:
        return {"summary": "Not a JWT-shaped value", "status": "FAILED", "error_code": "JWT_FORMAT_INVALID"}
    import base64
    try:
        header = json.loads(base64.urlsafe_b64decode(parts[0] + "=" * (-len(parts[0]) % 4)))
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, json.JSONDecodeError) as error:
        return {"summary": "JWT decode failed", "status": "FAILED", "error": str(error)}
    return {"summary": "JWT header and claims decoded", "extracted_facts": {"header": header, "claims": claims, "algorithm": header.get("alg"), "token_ref": session_store.put_secret(request.run_id, token, "jwt")}}


async def session_inspect(request: JobRequest) -> dict:
    session_name = str(request.arguments.get("session_name") or "default")
    facts = session_store.inspect(request.run_id, session_name)
    refs = {name: session_store.cookie_secret_ref(request.run_id, session_name, name) for name in facts.get("cookie_names", [])}
    return {"summary": "HTTP session inspected", "extracted_facts": {**facts, "secret_refs": {key: value for key, value in refs.items() if value}}}


async def session_list_secret_refs(request: JobRequest) -> dict:
    return {"summary": "Secret references listed", "extracted_facts": {"secret_refs": session_store.list_secret_refs(request.run_id)}}


async def jwt_clone_claims(request: JobRequest) -> dict:
    import base64
    try:
        token = session_store.get_secret(request.run_id, str(request.arguments["token_ref"]))
        parts = token.split(".")
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        claims.update(dict(request.arguments.get("claims") or {}))
        encoded = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).decode().rstrip("=")
        clone = f"{parts[0]}.{encoded}.{parts[2]}"
        return {"summary": "JWT claims cloned", "extracted_facts": {"token_ref": session_store.put_secret(request.run_id, clone, "jwt-clone"), "claim_keys": sorted(claims)}}
    except (KeyError, ValueError, json.JSONDecodeError, IndexError):
        return {"summary": "JWT claims clone failed", "status": "FAILED", "error_code": "JWT_CLONE_FAILED"}


async def jwt_sign(request: JobRequest) -> dict:
    import base64
    import hashlib
    import hmac
    try:
        secret = session_store.get_secret(request.run_id, str(request.arguments["secret_ref"]))
        header = dict(request.arguments.get("header") or {"alg": "HS256", "typ": "JWT"})
        claims = dict(request.arguments.get("claims") or {})
        def encode(value: dict) -> str:
            return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")
        signing_input = f"{encode(header)}.{encode(claims)}"
        signature = base64.urlsafe_b64encode(hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()).decode().rstrip("=")
        token_ref = session_store.put_secret(request.run_id, f"{signing_input}.{signature}", "jwt-signed")
        return {"summary": "JWT signed", "extracted_facts": {"token_ref": token_ref, "algorithm": header.get("alg"), "claim_keys": sorted(claims)}}
    except (KeyError, ValueError):
        return {"summary": "JWT signing failed", "status": "FAILED", "error_code": "JWT_SIGN_FAILED"}


async def http_session_set_cookie_ref(request: JobRequest) -> dict:
    try:
        session_name = str(request.arguments["session_name"])
        session_store.set_cookie_ref(request.run_id, session_name, str(request.arguments["cookie_name"]), str(request.arguments["secret_ref"]))
        return {"summary": "Session cookie updated from secret reference", "extracted_facts": session_store.inspect(request.run_id, session_name)}
    except (KeyError, ValueError):
        return {"summary": "Session cookie update failed", "status": "FAILED", "error_code": "SECRET_REF_NOT_FOUND"}


async def pcap_placeholder(request: JobRequest) -> dict:
    return {"summary": f"{request.tool} is policy-controlled and not available on this Runner", "status": "FAILED", "error_code": "TOOL_NOT_INSTALLED"}
