from pathlib import Path

import pytest
from fastapi import HTTPException

from app.config import settings
from app.workspace.paths import initialize_workspace, safe_child, workspace_for


def test_workspace_is_derived_only_from_run_id(tmp_path: Path) -> None:
    settings.workspace_root = tmp_path
    workspace = initialize_workspace("run-a")
    assert workspace == tmp_path / "run-a"
    assert (workspace / ".jobs").is_dir()
    with pytest.raises(HTTPException):
        workspace_for("../run-b")


def test_path_traversal_is_blocked(tmp_path: Path) -> None:
    workspace = tmp_path / "run-a"; workspace.mkdir()
    with pytest.raises(HTTPException): safe_child(workspace, "../secret")
    with pytest.raises(HTTPException): safe_child(workspace, "C:/secret")
