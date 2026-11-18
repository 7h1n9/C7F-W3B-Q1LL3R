"""The reproduction replay runs in the Muteki container, not the retired Runner.

Regression context (2026-11-10): ``fresh_reproduction`` dispatched through
``runner_client``, the legacy Kali Runner.  That service is not deployed
(``legacy_kali_runner=False``), so ``_require_enabled()`` raised
``RunnerRetiredError`` before anything executed:

    arena.fresh_reproduction events -> 37, all reason="RunnerRetiredError"
    solve_runs.fresh_reproduction_verified=1 -> 0 / 742

The commands were never Runner-specific -- the renderer emits plain shell -- so
they now run via ``docker exec`` in the Run's worker container.  These tests pin
the properties that make that safe, using a fake ``_docker`` so no container is
needed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import container_reproduction as cr
from app.services.container_reproduction import ContainerCommandRunner, container_name_for


def _completed(stdout: str = "", stderr: str = "", code: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=stdout, stderr=stderr)


@pytest.fixture
def fake_container(monkeypatch):
    """Stub out container creation and ``docker exec``."""

    calls: list[tuple] = []
    state = {"result": _completed("ok\n")}

    def _ensure(run_id, host_workspace, **kwargs):
        return SimpleNamespace(
            container=container_name_for(run_id),
            to_container_cwd=lambda host: "/home/kali/workspace",
        )

    def _fake_docker(*args, timeout=30.0):
        calls.append(args)
        return state["result"]

    monkeypatch.setattr(cr, "ensure_container", _ensure)
    monkeypatch.setattr(cr, "_docker", _fake_docker)
    return state, calls


def test_the_container_name_matches_the_solver_convention():
    """Reproduction must land in the SAME container the solver used, or it reads a
    different workspace than the one holding the evidence."""

    assert container_name_for("abc-123") == "muteki-run-abc-123"


def test_a_zero_exit_is_completed(fake_container):
    state, _ = fake_container
    state["result"] = _completed("hello\n")
    out = ContainerCommandRunner().run("r1", Path("/tmp/ws"), "echo hello")
    assert out["status"] == "COMPLETED"
    assert out["exit_code"] == 0
    assert out["body"] == "hello\n"


def test_a_nonzero_exit_is_failed_but_still_carries_the_output(fake_container):
    """``curl`` exits 7 on connection refused while still printing useful text.
    The status must say FAILED; the body must not be discarded."""

    state, _ = fake_container
    state["result"] = _completed(stdout="partial", stderr="curl: (7) refused", code=7)
    out = ContainerCommandRunner().run("r1", Path("/tmp/ws"), "curl x")
    assert out["status"] == "FAILED"
    assert out["exit_code"] == 7
    assert out["body"] == "partial"
    assert "refused" in out["stderr"]


def test_the_command_runs_through_sh_c_in_the_workspace(fake_container):
    """The rendered command is a shell string (pipes, &&, quoting).  It must be
    handed to ``sh -c`` as ONE argument, not split."""

    _, calls = fake_container
    ContainerCommandRunner().run("r1", Path("/tmp/ws"), "echo a | tr a b")
    args = calls[-1]
    assert "exec" in args and "sh" in args and "-c" in args
    assert args[args.index("-c") + 1] == "echo a | tr a b"
    assert "/home/kali/workspace" in args


def test_a_raising_exec_becomes_data_not_an_exception(fake_container, monkeypatch):
    """``execute`` records an opaque ``error`` if this raises, which is the exact
    failure mode being fixed.  A dead container must be a result."""

    def _boom(*args, **kwargs):
        raise RuntimeError("docker daemon gone")

    monkeypatch.setattr(cr, "_docker", _boom)
    out = ContainerCommandRunner().run("r1", Path("/tmp/ws"), "true")
    assert out["status"] == "FAILED"
    assert out["error_code"] == "CONTAINER_EXEC_RAISED"
    assert "docker daemon gone" in out["summary"]


def test_a_timeout_becomes_data_not_an_exception(fake_container, monkeypatch):
    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="docker", timeout=1)

    monkeypatch.setattr(cr, "_docker", _timeout)
    out = ContainerCommandRunner().run("r1", Path("/tmp/ws"), "sleep 999")
    assert out["status"] == "FAILED"
    assert out["error_code"] == "CONTAINER_EXEC_RAISED"


def test_huge_output_is_truncated_but_keeps_both_ends(fake_container):
    """A ``sqlmap --dump`` can be megabytes and it all lands in a JSON file.  The
    flag can be at either end, so truncation must not simply cut the tail."""

    state, _ = fake_container
    body = "HEAD" + ("x" * (cr.MAX_CAPTURE_CHARS * 2)) + "TAIL"
    state["result"] = _completed(body)
    out = ContainerCommandRunner().run("r1", Path("/tmp/ws"), "cat big")
    assert len(out["body"]) < len(body)
    assert out["body"].startswith("HEAD")
    assert out["body"].endswith("TAIL")
    assert "elided" in out["body"]


def test_run_async_returns_the_same_shape(fake_container):
    """The caller is ``async``; the bridge must not lose fields."""

    import asyncio

    state, _ = fake_container
    state["result"] = _completed("async body")
    out = asyncio.run(ContainerCommandRunner().run_async("r1", Path("/tmp/ws"), "echo"))
    assert out["status"] == "COMPLETED"
    assert out["body"] == "async body"


# ── the executor no longer imports the Runner ───────────────────────────────


def test_the_executor_does_not_reference_the_retired_runner():
    """A re-introduced import would silently restore the 37-error behaviour."""

    source = (
        Path(__file__).resolve().parents[1]
        / "app" / "services" / "fresh_reproduction.py"
    ).read_text(encoding="utf-8")
    code_lines = [
        line for line in source.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    # Docstrings mention the old module by name on purpose; only real code counts.
    assert not any("from app.services.runner_client" in line for line in code_lines)
    assert not any("runner_client." in line for line in code_lines)


# ── PoC fallback extraction (2026-11-10) ────────────────────────────────────
#
# Muteki workers run shell directly, so a Run has no structured ``http_request``
# ToolCall to replay.  On run 9847adf6 the extractor returned 0 steps and the
# replay blocked before executing anything -- even though the worker had written
# ``muteki/exploit2.py`` and the evidence was on disk the whole time.


def _extractor():
    from app.services.reports import SolutionPathExtractor

    return SolutionPathExtractor()


def test_a_worker_poc_script_becomes_a_replay_step(tmp_path):
    (tmp_path / "muteki").mkdir()
    poc = tmp_path / "muteki" / "exploit2.py"
    poc.write_text("print('flag{test}')\n", encoding="utf-8")
    run = SimpleNamespace(workspace_path=str(tmp_path))

    steps = _extractor()._workspace_poc_steps(run)

    assert len(steps) == 1
    assert steps[0].tool_name == "workspace_poc"
    assert steps[0].normalized_arguments["path"] == "muteki/exploit2.py"
    assert steps[0].normalized_arguments["interpreter"] == "python3"
    # The command must be relative to the workspace and contain the placeholder
    # the caller replaces with the container-side path.
    assert "{workspace}" in steps[0].manual_command


def test_the_newest_poc_wins(tmp_path):
    """``exploit.py`` then ``exploit2.py``: the second is the one that worked."""

    import os
    import time

    (tmp_path / "muteki").mkdir()
    first = tmp_path / "muteki" / "exploit.py"
    first.write_text("v1\n", encoding="utf-8")
    os.utime(first, (time.time() - 600, time.time() - 600))
    second = tmp_path / "muteki" / "exploit2.py"
    second.write_text("v2\n", encoding="utf-8")

    steps = _extractor()._workspace_poc_steps(SimpleNamespace(workspace_path=str(tmp_path)))
    assert steps[0].normalized_arguments["path"] == "muteki/exploit2.py"


def test_an_empty_script_is_not_a_replay_step(tmp_path):
    """A zero-byte ``exploit.py`` is a stub, not evidence."""

    (tmp_path / "muteki").mkdir()
    (tmp_path / "muteki" / "exploit.py").write_text("", encoding="utf-8")

    assert _extractor()._workspace_poc_steps(SimpleNamespace(workspace_path=str(tmp_path))) == []


def test_no_workspace_means_no_steps():
    assert _extractor()._workspace_poc_steps(SimpleNamespace(workspace_path="")) == []
    assert _extractor()._workspace_poc_steps(
        SimpleNamespace(workspace_path=str(Path("/definitely/not/here")))
    ) == []


def test_the_worker_marker_is_not_replayable():
    """``muteki_native_worker`` carries no request.  Replaying it rendered
    ``curl -i -X GET "{{target_url}}"`` -- an empty URL that reproduces nothing."""

    from app.services.reports import SolutionPathExtractor

    assert "muteki_native_worker" in SolutionPathExtractor._EXCLUDED_TOOLS


def test_the_renderer_handles_the_workspace_poc_tool():
    from app.services.reproduction_commands import reproduction_command_renderer

    command = reproduction_command_renderer.render(
        "workspace_poc", {"path": "muteki/exploit.py", "interpreter": "python3"}
    )
    assert command == "cd {workspace} && python3 muteki/exploit.py"


def test_a_missing_module_is_named_not_just_nonzero(fake_container):
    """``ModuleNotFoundError`` means the replay never ran the exploit -- the image
    lacks a library.  That must not look like "the chain did not reproduce".

    Measured on run 9847adf6: ``No module named 'pyotp'`` surfaced only as
    ``status: FAILED, exit: 1``.
    """

    state, _ = fake_container
    state["result"] = _completed(
        stderr="Traceback (most recent call last):\nModuleNotFoundError: No module named 'pyotp'\n",
        code=1,
    )
    out = ContainerCommandRunner().run("r1", Path("/tmp/ws"), "python3 poc.py")
    assert out["status"] == "FAILED"
    assert out["error_code"] == "MISSING_DEPENDENCY"
    assert out["missing_module"] == "pyotp"
    assert "pyotp" in out["summary"]


def test_an_ordinary_failure_is_not_mistaken_for_a_missing_module(fake_container):
    state, _ = fake_container
    state["result"] = _completed(stderr="curl: (7) refused", code=7)
    out = ContainerCommandRunner().run("r1", Path("/tmp/ws"), "curl x")
    assert out["error_code"] == "NONZERO_EXIT"
    assert out["missing_module"] is None
