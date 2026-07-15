
import hashlib

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
    text = raw.decode(errors="replace")
    lines = text.splitlines()
    start = max(1, int(request.arguments.get("start_line", 1)))
    end = int(request.arguments.get("end_line", len(lines)))
    end = max(start, min(end, len(lines)))
    max_chars = max(1, min(int(request.arguments.get("max_chars", settings.max_output_bytes)), settings.max_output_bytes))
    content = "\n".join(lines[start - 1 : end])[:max_chars]
    relative = str(path.relative_to(workspace)).replace("\\", "/")
    return {
        "path": relative,
        "start_line": start,
        "end_line": end,
        "content": content,
        "content_excerpt": content,
        "truncated": len(content) < len("\n".join(lines[start - 1 : end])),
        "content_sha256": hashlib.sha256(raw).hexdigest(),
        "summary": f"Read {relative}:{start}-{end}",
        "extracted_facts": {"path": relative, "start_line": start, "end_line": end, "content_sha256": hashlib.sha256(raw).hexdigest()},
    }


async def file_search(request: JobRequest) -> dict:
    workspace = workspace_for(request.run_id)
    needle = str(request.arguments.get("query", "")).lower()
    maximum = min(int(request.arguments.get("max_results", 20)), 100)
    results = []
    for path in workspace.rglob("*"):
        if not path.is_file() or len(results) >= maximum:
            continue
        relative = str(path.relative_to(workspace))
        content = path.read_text(encoding="utf-8", errors="ignore")[:settings.max_output_bytes]
        if needle in path.name.lower():
            results.append({"path": relative, "line": 1, "snippet": path.name})
        else:
            for number, line in enumerate(content.splitlines(), 1):
                if needle in line.lower():
                    results.append({"path": relative, "line": number, "snippet": line[:1000]})
                    break
    return {"matching_paths": [item["path"] for item in results], "match_snippets": results, "line_numbers": [{"path": item["path"], "line": item["line"]} for item in results], "matches": results, "summary": f"Found {len(results)} matching files"}
