import asyncio
import hashlib
import json
import os
import secrets
import shutil
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.responses import JSONResponse

from app.config import settings
from app.models import JobRequest
from app.service import job_service
from app.workspace.paths import DOWNLOAD_DIRS, UPLOAD_DIRS, initialize_workspace, safe_child, workspace_for
from app.executors.session_store import session_store


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.require_safe_production_token()
    await job_service.recover()
    yield


app = FastAPI(title="CTF Kali Runner", version="0.2.0", lifespan=lifespan)


def _error(code: str, message: str, status_code: int, stage: str, details: object = None) -> dict:
    return {"code": code, "message": message, "stage": stage, "retryable": status_code >= 500, "diagnostic_id": secrets.token_hex(16), "tool_execution_completed": False, "details": details if isinstance(details, dict) else {"detail": details}}


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, error: RequestValidationError) -> JSONResponse:
    return JSONResponse(content=_error("MCP_VALIDATION_FAILED", "Runner request validation failed.", 422, "VALIDATION", {"errors": error.errors()}), status_code=422)


@app.exception_handler(HTTPException)
async def http_error(_: Request, error: HTTPException) -> JSONResponse:
    return JSONResponse(content=_error("RUNNER_JOB_FAILED", str(error.detail), error.status_code, "EXECUTION", {"detail": error.detail}), status_code=error.status_code)


@app.exception_handler(Exception)
async def unhandled_error(_: Request, error: Exception) -> JSONResponse:
    return JSONResponse(content=_error("RUNNER_UNAVAILABLE", "Runner request failed.", 503, "RUNNER", {"error": str(error)[:1000]}), status_code=503)


@app.get("/health")
async def health() -> dict:
    capabilities = await capability_registry()
    return {"status": "ok", "execution_backend": "KaliVmExecutionBackend", "build": {"component": "runner", "git_sha": os.getenv("GIT_SHA", "unknown"), "build_id": os.getenv("RUNNER_BUILD_ID", os.getenv("BUILD_ID", "dev")), "mcp_schema_version": "mcp-v1"}, "capabilities": capabilities}


async def capability_registry() -> dict:
    tools = [
        "http_request", "http_session_request", "http_extract", "whatweb_fingerprint", "js_asset_analyze", "source_map_analyze",
        "file_type", "strings_extract", "archive_list", "content_discovery", "jwt_inspect", "session_inspect", "session_list_secret_refs", "jwt_clone_claims", "jwt_sign", "http_session_set_cookie_ref", "file_read",
        "file_search", "python_run", "script_run", "sandbox_exec", "pcap_metadata", "pcap_protocols", "pcap_query", "pcap_tcp_stream",
        "pcap_http_objects", "pcap_dns_summary", "pcap_credentials", "request_capture", "sqlmap_detect", "sqlmap_run", "sql_injection_probe", "sql_boolean_compare", "sql_union_probe", "oracle_probe_matrix", "boolean_config_extract",
        "nikto_scan", "binwalk_scan", "exiftool_metadata",
    ]
    placeholder_tools = {"pcap_tcp_stream", "pcap_http_objects", "pcap_dns_summary", "pcap_credentials", "nmap_service_probe", "nikto_scan", "binwalk_scan", "exiftool_metadata"}
    command_map = {"python_run": ("python", "--version"), "script_run": ("python", "--version"), "sandbox_exec": ("file", "--version"), "sqlmap_detect": ("sqlmap", "--version"), "sqlmap_run": ("sqlmap", "--version")}
    async def self_test(command: tuple[str, str] | None) -> tuple[bool, str | None]:
        if command is None:
            return True, None
        executable = shutil.which(command[0])
        if not executable:
            return False, None
        try:
            completed = await asyncio.to_thread(subprocess.run, [executable, command[1]], capture_output=True, text=True, timeout=3, check=False)
            return completed.returncode == 0, (completed.stdout or completed.stderr).strip()[:120]
        except (OSError, subprocess.SubprocessError):
            return False, None
    tool_rows = []
    for name in tools:
        ok, version = await self_test(command_map.get(name))
        implemented = name not in placeholder_tools
        installed = ok if name in command_map else True
        tool_rows.append({"name": name, "implemented": implemented, "installed": installed, "enabled": implemented and installed, "available": implemented and installed, "version": version or ("policy-wrapper" if implemented else None), "self_test_ok": ok and implemented, "last_self_check": None})
    return {
        "tools": tool_rows,
        "binaries": {name: bool(shutil.which(name)) for name in ("tshark", "capinfos", "ffuf", "feroxbuster", "sqlmap", "nmap", "nikto", "binwalk", "exiftool")},
        "interpreters": {name: bool(shutil.which(name)) for name in ("python", "node", "bash")},
        "network_enforcement": {"mode": "controlled_proxy" if os.environ.get("RUNNER_ENFORCED_PROXY_URL") else "none", "available": bool(os.environ.get("RUNNER_ENFORCED_PROXY_URL")), "enforced": bool(os.environ.get("RUNNER_ENFORCED_PROXY_URL")), "os_level": False, "target_allowlist": bool(os.environ.get("RUNNER_ENFORCED_PROXY_URL")), "reason": "Controlled proxy is configured." if os.environ.get("RUNNER_ENFORCED_PROXY_URL") else "No namespace/nftables/iptables/proxy enforcement is available on this host."},
    }


def require_token(x_runner_token: str | None = Header(default=None)) -> None:
    if not settings.api_token or not x_runner_token or not secrets.compare_digest(x_runner_token, settings.api_token):
        raise HTTPException(401, detail="runner token required")


@app.get("/api/v1/capabilities")
async def capabilities(_: None = Depends(require_token)) -> dict:
    return await capability_registry()


@app.post("/api/v1/capabilities/self-test")
async def capabilities_self_test(_: None = Depends(require_token)) -> dict:
    registry = await capability_registry()
    for tool in registry["tools"]:
        tool["last_self_check"] = __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat()
    return registry


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


@app.post("/api/v1/workspaces/{run_id}")
async def create_workspace(run_id: str, _: None = Depends(require_token)) -> dict:
    initialize_workspace(run_id)
    return {"run_id": run_id, "status": "ready"}


@app.put("/api/v1/workspaces/{run_id}/files/{relative_path:path}")
async def upload_file(run_id: str, relative_path: str, request: Request, x_content_sha256: str | None = Header(default=None), _: None = Depends(require_token)) -> dict:
    workspace = initialize_workspace(run_id)
    path = safe_child(workspace, relative_path)
    normalized = relative_path.replace("\\", "/")
    if normalized.split("/", 1)[0] not in UPLOAD_DIRS and normalized not in UPLOAD_DIRS:
        raise HTTPException(403, detail="file upload is outside the Run Workspace input/output areas")
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > settings.max_upload_bytes:
        raise HTTPException(413, detail="file exceeds upload limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    size, hasher = 0, hashlib.sha256()
    try:
        with temp.open("wb") as handle:
            async for chunk in request.stream():
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise HTTPException(413, detail="file exceeds upload limit")
                hasher.update(chunk)
                handle.write(chunk)
        actual = hasher.hexdigest()
        if x_content_sha256 and not secrets.compare_digest(actual, x_content_sha256.lower()):
            raise HTTPException(422, detail="SHA-256 mismatch")
        temp.replace(path)
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)
    return {"path": relative_path.replace("\\", "/"), "size": size, "sha256": actual}


@app.get("/api/v1/workspaces/{run_id}/files/{relative_path:path}")
async def download_file(run_id: str, relative_path: str, _: None = Depends(require_token)) -> FileResponse:
    workspace = workspace_for(run_id)
    path = safe_child(workspace, relative_path)
    if not path.is_file() or relative_path.replace("\\", "/").split("/", 1)[0] not in DOWNLOAD_DIRS:
        raise HTTPException(404, detail="artifact not found")
    return FileResponse(path, headers={"X-Artifact-Size": str(path.stat().st_size), "X-Artifact-SHA256": digest(path)})


@app.get("/api/v1/workspaces/{run_id}/manifest")
async def manifest(run_id: str, _: None = Depends(require_token)) -> dict:
    workspace = workspace_for(run_id)
    files = []
    if workspace.exists():
        for path in workspace.rglob("*"):
            if path.is_file() and not path.is_symlink():
                files.append({"path": str(path.relative_to(workspace)).replace("\\", "/"), "size": path.stat().st_size, "sha256": digest(path)})
    return {"run_id": run_id, "files": files}


@app.delete("/api/v1/workspaces/{run_id}")
async def delete_workspace(run_id: str, _: None = Depends(require_token)) -> dict:
    session_store.clear_run(run_id)
    workspace = workspace_for(run_id)
    if workspace.exists():
        import shutil
        shutil.rmtree(workspace)
    return {"run_id": run_id, "status": "deleted"}


@app.delete("/api/v1/sessions/{run_id}")
async def clear_sessions(run_id: str, _: None = Depends(require_token)) -> dict:
    session_store.clear_run(run_id)
    return {"run_id": run_id, "status": "cleared"}


@app.post("/api/v1/jobs")
async def create_job(request: JobRequest, _: None = Depends(require_token)) -> dict:
    job = await job_service.create(request)
    return {"job_id": job.job_id, "status": job.status}


@app.get("/api/v1/jobs/{job_id}")
async def get_job(job_id: str, _: None = Depends(require_token)) -> dict:
    return (await job_service.get(job_id)).model_dump()


@app.post("/api/v1/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, _: None = Depends(require_token)) -> dict:
    return (await job_service.cancel(job_id)).model_dump()


@app.get("/api/v1/jobs/{job_id}/events")
async def job_events(job_id: str, _: None = Depends(require_token)) -> StreamingResponse:
    async def stream():
        while True:
            job = await job_service.get(job_id)
            yield f"event: job.status\ndata: {json.dumps({'status': job.status, 'error': job.error})}\n\n"
            if job.status.value in {"COMPLETED", "FAILED", "CANCELLED"}:
                return
            await asyncio.sleep(1)
    return StreamingResponse(stream(), media_type="text/event-stream")
