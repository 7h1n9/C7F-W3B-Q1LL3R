"""Allowlisted offline executable runner.  It never invokes a shell."""
from __future__ import annotations

import asyncio
import os
import shutil
import time

from fastapi import HTTPException

from app.config import settings
from app.models import JobRequest
from app.workspace.paths import safe_child, workspace_for

OFFLINE_TOOLS = {"file", "strings", "grep", "sed", "awk", "jq", "xxd", "base64", "openssl", "unzip", "tar", "7z", "binwalk", "exiftool"}
FORBIDDEN_TOKENS = ("|", ">", "<", "&&", "||", ";", "`", "$()", "\n", "\r")


async def sandbox_exec(request: JobRequest) -> dict:
    executable = str(request.arguments.get("executable") or "")
    if executable not in OFFLINE_TOOLS or shutil.which(executable) is None:
        raise HTTPException(422, detail=f"TOOL_NOT_INSTALLED: executable is not an installed allowlisted offline tool: {executable}")
    args = request.arguments.get("args", [])
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise HTTPException(422, detail="args must be an array of strings")
    if any(any(token in item for token in FORBIDDEN_TOKENS) for item in args):
        raise HTTPException(422, detail="shell syntax is forbidden; pass argv items only")
    network_mode = str(request.arguments.get("network_mode") or "none")
    if network_mode != "none":
        raise HTTPException(403, detail="sandbox_exec only supports network_mode=none; use a network wrapper for targets")
    workspace = workspace_for(request.run_id)
    cwd = safe_child(workspace, str(request.arguments.get("cwd") or "scratch"))
    cwd.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy(); environment.update({"HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "", "NO_PROXY": "*"})
    started = time.perf_counter()
    process = await asyncio.create_subprocess_exec(executable, *args, cwd=str(cwd), env=environment, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=settings.job_timeout_seconds)
    except TimeoutError:
        process.kill(); await process.wait()
        raise HTTPException(408, detail="sandbox executable timed out")
    return {"exit_code": process.returncode, "stdout_excerpt": stdout[: settings.max_output_bytes].decode(errors="replace"), "stderr_excerpt": stderr[: settings.max_output_bytes].decode(errors="replace"), "output": stdout[: settings.max_output_bytes].decode(errors="replace"), "truncated": len(stdout) > settings.max_output_bytes or len(stderr) > settings.max_output_bytes, "network_targets": [], "runtime_ms": round((time.perf_counter() - started) * 1000), "summary": f"{executable} exited with {process.returncode}"}
