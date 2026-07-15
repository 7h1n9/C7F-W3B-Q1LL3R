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
    workspace = workspace_for(request.run_id)
    before = _snapshot(workspace)
    environment = os.environ.copy()
    environment.update({"CTF_NETWORK_MODE": network_mode, "CTF_ALLOWED_HOSTS": ",".join(request.allowed_hosts)})
    if network_mode == "none":
        environment.update({"HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "", "NO_PROXY": "*"})
    started = time.perf_counter()
    try:
        process = await asyncio.create_subprocess_exec(command, str(script), *args, cwd=str(workspace), env=environment, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill(); await process.wait()
            return {"exit_code": -1, "stdout_excerpt": "", "stderr_excerpt": "script timed out", "runtime_ms": round((time.perf_counter() - started) * 1000), "network_targets": request.allowed_hosts if network_mode == "target_allowlist" else [], "summary": "Script timed out", "error_code": "SCRIPT_TIMEOUT"}
    except FileNotFoundError as error:
        raise HTTPException(503, detail=f"interpreter not installed: {command}") from error
    after = _snapshot(workspace)
    generated = [{"path": path, "sha256": checksum} for path, checksum in after.items() if before.get(path) != checksum]
    stdout_excerpt = stdout[: settings.max_output_bytes].decode(errors="replace")
    stderr_excerpt = stderr[: settings.max_output_bytes].decode(errors="replace")
    return {"exit_code": process.returncode, "stdout_excerpt": stdout_excerpt, "stderr_excerpt": stderr_excerpt, "output": stdout_excerpt, "truncated": len(stdout) > settings.max_output_bytes or len(stderr) > settings.max_output_bytes, "generated_files": generated, "runtime_ms": round((time.perf_counter() - started) * 1000), "network_targets": request.allowed_hosts if network_mode == "target_allowlist" else [], "summary": f"{command} exited with {process.returncode}"}
