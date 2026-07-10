from pathlib import Path

from fastapi import HTTPException

from app.config import settings


def workspace_for(run_id: str, claimed_workspace: str) -> Path:
    root = settings.workspace_root.resolve()
    expected = (root / run_id).resolve()
    claimed = Path(claimed_workspace).resolve()
    if expected != claimed or expected.parent != root:
        raise HTTPException(403, detail="workspace does not belong to the requested run")
    return expected


def safe_child(workspace: Path, relative_path: str, allowed_prefix: str | None = None) -> Path:
    candidate = (workspace / relative_path).resolve()
    if candidate == workspace or workspace not in candidate.parents:
        raise HTTPException(403, detail="path traversal or absolute path is not allowed")
    if allowed_prefix and (workspace / allowed_prefix).resolve() not in candidate.parents:
        raise HTTPException(403, detail=f"path must remain under {allowed_prefix}")
    return candidate
