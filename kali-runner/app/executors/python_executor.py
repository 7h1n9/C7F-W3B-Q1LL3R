import asyncio

from fastapi import HTTPException

from app.config import settings
from app.models import JobRequest
from app.workspace.paths import safe_child, workspace_for


async def python_run(request: JobRequest) -> dict:
    workspace = workspace_for(request.run_id, request.workspace_path)
    script = safe_child(workspace, str(request.arguments.get("path", "")), "scripts")
    if script.suffix != ".py" or not script.is_file():
        raise HTTPException(400, detail="python_run only accepts existing scripts/*.py files")
    supplied = request.arguments.get("args", [])
    if not isinstance(supplied, list) or not all(isinstance(item, str) for item in supplied):
        raise HTTPException(422, detail="args must be an array of strings")
    process = await asyncio.create_subprocess_exec("python", str(script), *supplied, cwd=str(workspace), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
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
    return {"exit_code": process.returncode, "output": capped.decode(errors="replace"), "truncated": len(output) > len(capped), "summary": f"Python exited with {process.returncode}"}
