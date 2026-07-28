import asyncio
import os

from fastapi import HTTPException

from app.config import settings
from app.models import JobRequest
from app.workspace.paths import safe_child, workspace_for
from app.executors.script_validation import validate_python_source


async def python_run(request: JobRequest) -> dict:
    requested_mode = str(request.arguments.get("network_mode") or "none")
    if requested_mode != "none":
        return {
            "status": "FAILED",
            "error_code": "PYTHON_RUN_NETWORK_FORBIDDEN",
            "stage": "NETWORK_POLICY",
            "network_mode": requested_mode,
            "tool_execution_completed": False,
            "retryable": False,
            "summary": "python_run is offline-only; use script_run with target_allowlist for authorized network scripts.",
        }
    workspace = workspace_for(request.run_id)
    script = safe_child(workspace, str(request.arguments.get("path", "")), "scripts")
    if script.suffix != ".py" or not script.is_file():
        raise HTTPException(400, detail="python_run only accepts existing scripts/*.py files")
    errors = validate_python_source(script.read_text(encoding="utf-8", errors="replace"), require_result_contract=False)
    if errors:
        network_markers = ("urllib", "http", "socket", "requests", "httpx", "aiohttp", "asyncio", "ftplib", "smtplib", "websocket", "paramiko", "subprocess", "dns")
        return {
            "status": "FAILED",
            "error_code": "PYTHON_RUN_NETWORK_FORBIDDEN" if any(any(marker in item.lower() for marker in network_markers) for item in errors) else "PYTHON_RUN_SCRIPT_FORBIDDEN",
            "stage": "SCRIPT_VALIDATION",
            "network_mode": "none",
            "retryable": False,
            "tool_execution_completed": False,
            "summary": "python_run only accepts offline Python source",
            "validation_errors": errors[:20],
        }
    supplied = request.arguments.get("args", [])
    if not isinstance(supplied, list) or not all(isinstance(item, str) for item in supplied):
        raise HTTPException(422, detail="args must be an array of strings")
    environment = os.environ.copy()
    for key in list(environment):
        if key.lower().endswith("_proxy") or key.upper() in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}:
            environment.pop(key, None)
    environment.update({"CTF_NETWORK_MODE": "none", "CTF_ALLOWED_HOSTS": ""})
    process = await asyncio.create_subprocess_exec("python", "-I", str(script), *supplied, cwd=str(workspace), env=environment, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=settings.job_timeout_seconds)
    except TimeoutError:
        process.kill(); await process.wait()
        raise HTTPException(408, detail="script timed out")
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise
    capped = output[:settings.max_output_bytes]
    return {"exit_code": process.returncode, "network_mode": "none", "output": capped.decode(errors="replace"), "truncated": len(output) > len(capped), "summary": f"Python exited with {process.returncode}"}
