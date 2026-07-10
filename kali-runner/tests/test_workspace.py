from pathlib import Path

import pytest
from fastapi import HTTPException

from app.config import settings
from app.workspace.paths import safe_child, workspace_for


def test_workspace_rejects_other_run(tmp_path: Path) -> None:
    settings.workspace_root = tmp_path
    (tmp_path / "run-a").mkdir()
    with pytest.raises(HTTPException): workspace_for("run-a", str(tmp_path / "run-b"))


def test_path_traversal_is_blocked(tmp_path: Path) -> None:
    workspace = tmp_path / "run-a"; workspace.mkdir()
    with pytest.raises(HTTPException): safe_child(workspace, "../secret")
