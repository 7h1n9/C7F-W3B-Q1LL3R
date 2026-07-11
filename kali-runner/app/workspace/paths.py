from pathlib import Path, PurePosixPath

from fastapi import HTTPException

from app.config import settings

WORKSPACE_DIRS = ("source", "attachments", "requests", "responses", "scripts", "evidence", "outputs", "final", ".jobs")
DOWNLOAD_DIRS = {"responses", "outputs", "evidence", "final", "requests"}


def workspace_for(run_id: str) -> Path:
    if not run_id or "/" in run_id or "\\" in run_id or run_id in {".", ".."}:
        raise HTTPException(422, detail="invalid run ID")
    root = settings.workspace_root.resolve()
    workspace = (root / run_id).resolve()
    if workspace.parent != root:
        raise HTTPException(403, detail="workspace does not belong to the requested run")
    return workspace


def initialize_workspace(run_id: str) -> Path:
    workspace = workspace_for(run_id)
    for name in WORKSPACE_DIRS:
        (workspace / name).mkdir(parents=True, exist_ok=True)
    return workspace


def safe_child(workspace: Path, relative_path: str, allowed_prefix: str | None = None) -> Path:
    pure = PurePosixPath(relative_path.replace("\\", "/"))
    if not relative_path or pure.is_absolute() or ".." in pure.parts or "." in pure.parts or any(":" in part for part in pure.parts):
        raise HTTPException(403, detail="path traversal or absolute path is not allowed")
    candidate = workspace.joinpath(*pure.parts)
    resolved = candidate.resolve()
    if workspace not in resolved.parents:
        raise HTTPException(403, detail="path traversal or symbolic link escape is not allowed")
    if allowed_prefix and pure.parts[0] != allowed_prefix:
        raise HTTPException(403, detail=f"path must remain under {allowed_prefix}")
    return resolved
