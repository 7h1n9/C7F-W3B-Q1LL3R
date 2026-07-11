
from fastapi import HTTPException

from app.config import settings
from app.models import JobRequest
from app.workspace.paths import safe_child, workspace_for


async def file_read(request: JobRequest) -> dict:
    workspace = workspace_for(request.run_id)
    path = safe_child(workspace, str(request.arguments.get("path", "")))
    if not path.is_file():
        raise HTTPException(404, detail="file not found")
    raw = path.read_bytes()
    content = raw[:settings.max_output_bytes].decode(errors="replace")
    return {"path": str(path.relative_to(workspace)), "content": content, "truncated": len(raw) > len(content.encode()), "summary": f"Read {path.name}"}


async def file_search(request: JobRequest) -> dict:
    workspace = workspace_for(request.run_id)
    needle = str(request.arguments.get("query", "")).lower()
    maximum = min(int(request.arguments.get("max_results", 20)), 100)
    results = []
    for path in workspace.rglob("*"):
        if not path.is_file() or len(results) >= maximum:
            continue
        relative = str(path.relative_to(workspace))
        if needle in path.name.lower() or needle in path.read_text(encoding="utf-8", errors="ignore").lower()[:settings.max_output_bytes]:
            results.append(relative)
    return {"matches": results, "summary": f"Found {len(results)} matching files"}
