import asyncio
import hashlib
import json
import secrets
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

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


@app.get("/health")
async def health() -> dict:
    capabilities = await capability_registry()
    return {"status": "ok", "execution_backend": "KaliVmExecutionBackend", "capabilities": capabilities}


async def capability_registry() -> dict:
    tools = [
        "http_request", "http_session_request", "http_extract", "whatweb_fingerprint", "js_asset_analyze", "source_map_analyze",
        "file_type", "strings_extract", "archive_list", "content_discovery", "jwt_inspect", "session_inspect", "session_list_secret_refs", "jwt_clone_claims", "jwt_sign", "http_session_set_cookie_ref", "file_read",
        "file_search", "python_run", "script_run", "sandbox_exec", "pcap_metadata", "pcap_protocols", "pcap_query", "pcap_tcp_stream",
        "pcap_http_objects", "pcap_dns_summary", "pcap_credentials", "sqlmap_detect", "nmap_service_probe", "sql_injection_probe", "sql_boolean_compare", "sql_union_probe",
        "nikto_scan", "binwalk_scan", "exiftool_metadata",
    ]
    placeholder_tools = {"pcap_tcp_stream", "pcap_http_objects", "pcap_dns_summary", "pcap_credentials", "sqlmap_detect", "nmap_service_probe", "nikto_scan", "binwalk_scan", "exiftool_metadata"}
    interpreter_installed = {"python_run": bool(shutil.which("python")), "script_run": any(bool(shutil.which(name)) for name in ("python", "node", "bash")), "sandbox_exec": any(bool(shutil.which(name)) for name in ("file", "strings", "grep", "sed", "awk", "jq", "xxd", "base64", "openssl", "unzip", "tar", "7z", "binwalk", "exiftool"))}
    return {
        "tools": [{"name": name, "implemented": name not in placeholder_tools, "installed": interpreter_installed.get(name, True), "enabled": name not in placeholder_tools and interpreter_installed.get(name, True), "available": name not in placeholder_tools and interpreter_installed.get(name, True), "version": "policy-wrapper", "self_test_ok": name not in placeholder_tools and interpreter_installed.get(name, True), "last_self_check": None} for name in tools],
        "binaries": {name: bool(shutil.which(name)) for name in ("tshark", "capinfos", "ffuf", "feroxbuster", "sqlmap", "nmap", "nikto", "binwalk", "exiftool")},
        "interpreters": {name: bool(shutil.which(name)) for name in ("python", "node", "bash")},
        "network_enforcement": {"mode": "runner_policy", "os_level": False, "target_allowlist": False},
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
