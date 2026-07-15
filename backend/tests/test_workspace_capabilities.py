from pathlib import Path

import pytest

from app.core.exceptions import DomainError
from app.services.challenge_lessons import ChallengeLessonService
from app.services.workspace_policy import WorkspacePolicy, file_manifest
from app.tools.registry import load_tool_definitions


def test_workspace_policy_separates_read_only_generated_and_delete_areas(tmp_path: Path) -> None:
    policy = WorkspacePolicy("run-a", "attempt-a", "lease-a", tmp_path)
    (tmp_path / "source").mkdir()
    (tmp_path / "source" / "app.py").write_text("print('ok')", encoding="utf-8")
    assert policy.readable("source/app.py")[1] == "source/app.py"
    assert policy.writable("scripts/solve.py")[1] == "scripts/solve.py"
    assert policy.deletable("scratch/generated.txt")[1] == "scratch/generated.txt"
    with pytest.raises(DomainError) as error:
        policy.writable("source/app.py")
    assert error.value.code == "WORKSPACE_WRITE_FORBIDDEN"
    with pytest.raises(DomainError) as error:
        policy.deletable("attachments/input.bin")
    assert error.value.code == "WORKSPACE_DELETE_FORBIDDEN"
    assert file_manifest(tmp_path)[0]["backend_present"] is True


def test_new_execution_tools_are_schema_registered() -> None:
    definitions = load_tool_definitions()
    assert {"script_run", "sandbox_exec"}.issubset(definitions)
    assert definitions["script_run"].validate_arguments({"path": "scripts/solve.py", "interpreter": "python"})["interpreter"] == "python"
    with pytest.raises(DomainError):
        definitions["sandbox_exec"].validate_arguments({"executable": "file", "args": "not-an-array"})


def test_strategy_only_lesson_service_never_replays_by_default() -> None:
    service = ChallengeLessonService()
    assert service.mode == "strategy_only"
    assert "flag" not in str(service.extract)
