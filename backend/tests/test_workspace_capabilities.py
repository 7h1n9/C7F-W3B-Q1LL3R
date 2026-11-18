import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.exceptions import DomainError
from app.services.challenge_lessons import ChallengeLessonService
from app.services.workspace import create_workspace
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


def test_workspace_refresh_updates_target_without_deleting_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.services.workspace.get_settings",
        lambda: SimpleNamespace(workspace_root=tmp_path),
    )
    challenge = SimpleNamespace(
        id="challenge-1",
        name="target",
        challenge_type="WEB_TARGET",
        target_url="http://old.test/",
        allowed_hosts=["old.test"],
        flag_pattern=r"flag\{[^}]+\}",
        metadata_json={},
    )
    workspace = create_workspace("run-1", challenge)
    note = workspace / "notes" / "evidence.md"
    note.write_text("preserve me", encoding="utf-8")

    challenge.target_url = "http://new.test/"
    challenge.allowed_hosts = ["new.test"]
    create_workspace("run-1", challenge)

    manifest = json.loads((workspace / "challenge.json").read_text(encoding="utf-8"))
    assert manifest["target_url"] == "http://new.test/"
    assert manifest["allowed_hosts"] == ["new.test"]
    assert note.read_text(encoding="utf-8") == "preserve me"


def test_agents_md_opens_with_recon_discipline_in_the_first_screen(
    tmp_path: Path, monkeypatch
) -> None:
    """The reconnaissance block must be readable before the Worker acts.

    Workers open ``AGENTS.md`` with ``... | head -50`` (seen verbatim in
    ``run_events`` for run ``c88aaf79``), so a block placed after the recipe or
    the hint text is never read.  Measured 2026-10-29 on that Run: 215 tool
    commands, no flag, and its first moves were guesses at filenames
    (``for p in post.php commitbug.php about``).
    """

    monkeypatch.setattr(
        "app.services.workspace.get_settings",
        lambda: SimpleNamespace(workspace_root=tmp_path),
    )
    challenge = SimpleNamespace(
        id="challenge-recon",
        name="[CISCN2019 华东北赛区]Web6",
        challenge_type="WEB_TARGET",
        target_url="http://box.test/",
        allowed_hosts=["box.test"],
        flag_pattern=r"flag\{[^}]+\}",
        metadata_json={},
    )

    workspace = create_workspace("run-recon", challenge)
    text = (workspace / "AGENTS.md").read_text(encoding="utf-8")

    first_screen = "\n".join(text.splitlines()[:40])
    assert "Before your first payload" in first_screen
    assert "At most 5 flag paths" in text
    assert "Follow links instead of inventing filenames" in text

    # Ordering contract: run policy first (it sets the rules of engagement),
    # then the discipline, then everything that is reference material.
    assert text.index("# Run Policy") < text.index("Before your first payload")
    if "## Code execution checklist" in text:
        assert text.index("Before your first payload") < text.index(
            "## Code execution checklist"
        )


def test_recon_discipline_does_not_prescribe_a_scanner(tmp_path: Path, monkeypatch) -> None:
    """The block replaces guessing with a fixed pass; it must not invite fuzzing.

    The lane goal already forbids ``ffuf``/``dirb``/``gobuster`` because those
    burned entire rounds (see ``TECHNIQUE_GOAL_MAX_CHARS`` history).  A recon
    checklist that reintroduces them would undo that fix from the other side.
    """

    monkeypatch.setattr(
        "app.services.workspace.get_settings",
        lambda: SimpleNamespace(workspace_root=tmp_path),
    )
    challenge = SimpleNamespace(
        id="challenge-recon-2",
        name="WarmUp",
        challenge_type="WEB_TARGET",
        target_url="http://box2.test/",
        allowed_hosts=["box2.test"],
        flag_pattern=r"flag\{[^}]+\}",
        metadata_json={},
    )

    workspace = create_workspace("run-recon-2", challenge)
    text = (workspace / "AGENTS.md").read_text(encoding="utf-8")

    head = text[text.index("Before your first payload"):text.index("Before your first payload") + 400]
    for scanner in ("ffuf", "dirb", "gobuster"):
        assert scanner not in head
    assert "One fixed reconnaissance pass" in text
