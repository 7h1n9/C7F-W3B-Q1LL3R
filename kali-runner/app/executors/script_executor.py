"""Bounded, argv-only script execution for a Run Workspace."""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
from pathlib import Path

from fastapi import HTTPException

from app.config import settings
from app.models import JobRequest
from app.workspace.paths import safe_child, workspace_for
from app.executors.script_validation import validate_python_source
from app.executors.target_allowlist import TargetAllowlistProxy, enforced_proxy_available

INTERPRETERS = {"python": ("python", {".py"}), "node": ("node", {".js", ".mjs", ".cjs"}), "bash": ("bash", {".sh"})}


def _snapshot(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for directory in ("outputs", "evidence", "responses", "final"):
        base = root / directory
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file() and not path.is_symlink():
                result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _validate_args(arguments: dict) -> tuple[Path, str, list[str], str, int]:
    interpreter = str(arguments.get("interpreter") or "").lower()
    if interpreter not in INTERPRETERS:
        raise HTTPException(422, detail="interpreter must be one of python, node, bash")
    workspace = workspace_for(str(arguments.get("run_id") or ""))
    raw_path = str(arguments.get("path") or "")
    script = safe_child(workspace, raw_path)
    normalized = raw_path.replace("\\", "/")
    if not (normalized.startswith("scripts/") or normalized.startswith("scratch/scripts/")):
        raise HTTPException(403, detail="script_run only accepts scripts/** or scratch/scripts/**")
    command, suffixes = INTERPRETERS[interpreter]
    if script.suffix.lower() not in suffixes or not script.is_file():
        raise HTTPException(400, detail=f"script_run requires an existing {interpreter} script")
    if interpreter == "python":
        errors = validate_python_source(script.read_text(encoding="utf-8", errors="replace"), require_result_contract=False)
        if errors:
            raise HTTPException(422, detail="SCRIPT_VALIDATION_FAILED: " + "; ".join(errors[:20]))
    args = arguments.get("args", [])
    if not isinstance(args, list) or len(args) > 64 or not all(isinstance(item, str) and len(item) <= 4096 for item in args):
        raise HTTPException(422, detail="args must be an array of bounded strings")
    network_mode = str(arguments.get("network_mode") or "none")
    if network_mode not in {"none", "target_allowlist"}:
        raise HTTPException(422, detail="network_mode must be none or target_allowlist")
    timeout = max(1, min(int(arguments.get("timeout_seconds", settings.job_timeout_seconds)), settings.job_timeout_seconds))
    return script, command, args, network_mode, timeout


async def script_run(request: JobRequest) -> dict:
    arguments = {**request.arguments, "run_id": request.run_id}
    script, command, args, network_mode, timeout = _validate_args(arguments)
    if network_mode == "target_allowlist" and not request.allowed_hosts:
        raise HTTPException(403, detail="target_allowlist requires challenge.allowed_hosts")
    if network_mode == "target_allowlist" and not (os.environ.get("RUNNER_ENFORCED_PROXY_URL") or enforced_proxy_available()):
        raise HTTPException(503, detail="TARGET_NETWORK_ENFORCEMENT_UNAVAILABLE: target network enforcement is not available on this Runner")
    workspace = workspace_for(request.run_id)
    before = _snapshot(workspace)
    environment = os.environ.copy()
    environment.update({"CTF_NETWORK_MODE": network_mode, "CTF_ALLOWED_HOSTS": ",".join(request.allowed_hosts), "CTF_JOB_ID": str(request.arguments.get("job_id") or "")})
    if network_mode == "none":
        environment.update({"HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "", "NO_PROXY": "*"})
    proxy: TargetAllowlistProxy | None = None
    if network_mode == "target_allowlist" and os.environ.get("RUNNER_ENFORCED_PROXY_URL"):
        environment.update({"HTTP_PROXY": os.environ["RUNNER_ENFORCED_PROXY_URL"], "HTTPS_PROXY": os.environ["RUNNER_ENFORCED_PROXY_URL"], "ALL_PROXY": os.environ["RUNNER_ENFORCED_PROXY_URL"]})
        environment.update({"CTF_TARGET_ALLOWLIST_ENFORCED": "1"})
    elif network_mode == "target_allowlist":
        proxy = TargetAllowlistProxy(request.allowed_hosts)
        await proxy.start()
        environment.update({"HTTP_PROXY": proxy.url, "HTTPS_PROXY": proxy.url, "ALL_PROXY": proxy.url, "NO_PROXY": "", "CTF_TARGET_ALLOWLIST_ENFORCED": "1"})
    started = time.perf_counter()
    try:
        try:
            process = await asyncio.create_subprocess_exec(command, str(script), *args, cwd=str(workspace), env=environment, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            except TimeoutError:
                process.kill(); await process.wait()
                return {"exit_code": -1, "stdout_excerpt": "", "stderr_excerpt": "script timed out", "runtime_ms": round((time.perf_counter() - started) * 1000), "network_targets": request.allowed_hosts if network_mode == "target_allowlist" else [], "summary": "Script timed out", "error_code": "SCRIPT_TIMEOUT"}
            except asyncio.CancelledError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=3)
                except asyncio.TimeoutError:
                    process.kill(); await process.wait()
                raise
        except FileNotFoundError as error:
            raise HTTPException(503, detail=f"interpreter not installed: {command}") from error
    finally:
        if proxy is not None:
            await proxy.close()
    after = _snapshot(workspace)
    generated = [{"path": path, "sha256": checksum} for path, checksum in after.items() if before.get(path) != checksum]
    stdout_excerpt = stdout[: settings.max_output_bytes].decode(errors="replace")
    stderr_excerpt = stderr[: settings.max_output_bytes].decode(errors="replace")
    return {"exit_code": process.returncode, "stdout_excerpt": stdout_excerpt, "stderr_excerpt": stderr_excerpt, "output": stdout_excerpt, "truncated": len(stdout) > settings.max_output_bytes or len(stderr) > settings.max_output_bytes, "generated_files": generated, "runtime_ms": round((time.perf_counter() - started) * 1000), "network_targets": request.allowed_hosts if network_mode == "target_allowlist" else [], "summary": f"{command} exited with {process.returncode}"}
