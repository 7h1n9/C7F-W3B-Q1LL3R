"""Contract tests for the vendored CLI drivers and the app-layer adapters.

The app-layer engines are thin: argv, result parsing and failure classification
all come from ``muteki.solver.cli_driver``.  These tests pin that delegation so a
future "improvement" cannot quietly re-inline hand-written flags or start dumping
raw vendor JSON into a Worker's output.

No CLI binary is executed: the process boundary is replaced with a fake.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from app.solver.muteki.graph import Intent
from app.solver.muteki.worker.engine import get_engine
from app.solver.muteki.worker.engines import ENGINE_CLASSES


def _intent() -> Intent:
    return Intent(
        intent_id="i1",
        description="read the config",
        status="OPEN",
        claimed_by=None,
        created_at="2026-01-01T00:00:00",
        payload={},
    )


class _Completed:
    """Stand-in for the asyncio subprocess result."""

    def __init__(self, stdout: str, stderr: str = "", returncode: int = 0) -> None:
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def _patch_process(monkeypatch: pytest.MonkeyPatch, outcome: object) -> None:
    from app.solver.muteki.worker.engines import cli

    if isinstance(outcome, Exception):

        def fake(*_args: object, **_kwargs: object) -> object:
            raise outcome

    else:

        def fake(*_args: object, **_kwargs: object) -> object:
            return outcome

    monkeypatch.setattr(cli, "_run_command", fake)


def _execute(engine: object, monkeypatch: pytest.MonkeyPatch, outcome: object):
    _patch_process(monkeypatch, outcome)
    with tempfile.TemporaryDirectory() as workspace:
        return asyncio.run(engine.execute(_intent(), workspace))  # type: ignore[attr-defined]


CLAUDE_ENVELOPE = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": "sess-abc",
        "num_turns": 7,
        "duration_ms": 4200,
        "total_cost_usd": 0.0123,
        "usage": {
            "input_tokens": 1200,
            "output_tokens": 340,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 100,
        },
        "result": "Found an exposed .git directory at /static/.git/.",
    }
)


# ── engine registry ─────────────────────────────────────────────────────────


def test_the_registry_exposes_exactly_the_three_cli_engines() -> None:
    assert sorted(ENGINE_CLASSES) == ["claude", "codex", "cursor"]
    assert get_engine("claude").engine_type() == "claude"
    assert get_engine("CURSOR").engine_type() == "cursor"
    # The codex argument is accepted for legacy callers and deliberately ignored.
    assert get_engine("codex", cli=True).engine_type() == "codex"


def test_an_unknown_engine_is_rejected_by_name() -> None:
    with pytest.raises(ValueError):
        get_engine("gpt-5")


# ── argv comes from the vendored drivers ────────────────────────────────────


def test_claude_worker_argv_is_driver_generated() -> None:
    argv = get_engine("claude").build_argv("probe the target")
    assert "-p" in argv or "--print" in argv
    assert "--output-format" in argv
    assert "json" in argv
    # A non-interactive Worker launch cannot wait on a permission prompt.
    assert "--dangerously-skip-permissions" in argv
    assert argv[-1] == "probe the target"


def test_claude_isolation_flags_are_conditional_not_unconditional() -> None:
    # Both surfaces are available by default; the isolation flags appear only
    # when the launch actually denies them.
    online = get_engine("claude").build_argv("probe")
    assert "--strict-mcp-config" not in online

    sealed = ENGINE_CLASSES["claude"](web_access=False, kb_access=False)
    offline = sealed.build_argv("probe")
    assert "--strict-mcp-config" in offline
    assert any("mcpServers" in argument for argument in offline)
    assert "WebSearch" in offline and "WebFetch" in offline


def test_cursor_worker_argv_is_driver_generated() -> None:
    argv = get_engine("cursor").build_argv("probe the target")
    assert "-p" in argv or "--print" in argv
    assert "--force" in argv
    assert "--trust" in argv
    assert argv[-1] == "probe the target"


def test_codex_worker_argv_still_uses_the_exec_json_form() -> None:
    argv = get_engine("codex").build_argv("probe the target")
    assert "exec" in argv
    assert "--json" in argv
    assert argv[-1] == "probe the target"


def test_an_explicit_executable_replaces_only_argv0() -> None:
    baseline = get_engine("claude").build_argv("hi")
    pinned = ENGINE_CLASSES["claude"](executable="/opt/pinned/claude")
    argv = pinned.build_argv("hi")
    assert argv[0] == "/opt/pinned/claude"
    assert len(argv) == len(baseline)
    assert argv[1:] == baseline[1:]


def test_health_check_requires_a_runnable_binary() -> None:
    assert ENGINE_CLASSES["claude"](executable=sys.executable).health_check() is True
    missing = str(Path(tempfile.gettempdir()) / f"definitely-absent-{uuid.uuid4().hex}")
    assert ENGINE_CLASSES["claude"](executable=missing).health_check() is False


# ── result parsing ──────────────────────────────────────────────────────────


def test_execute_parses_the_vendor_envelope_instead_of_dumping_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _execute(get_engine("claude"), monkeypatch, _Completed(CLAUDE_ENVELOPE))
    assert result.success is True
    # The Worker's output must be the assistant answer, never the raw envelope.
    assert result.output == "Found an exposed .git directory at /static/.git/."
    assert result.engine_type == "claude"

    metadata = result.metadata
    assert metadata["driver"] == "claude"
    assert metadata["session"] == "sess-abc"
    assert metadata["num_turns"] == 7
    assert metadata["cost_usd"] == pytest.approx(0.0123)
    assert metadata["returncode"] == 0

    usage = metadata["usage"]
    # The driver bills cache read+write INTO input_tokens and reports the buckets
    # separately so they can be backed out: 1200 + 900 + 100.
    assert usage["input_tokens"] == 2200
    assert usage["output_tokens"] == 340
    assert usage["cache_read_tokens"] == 900
    assert usage["cache_write_tokens"] == 100
    assert usage["estimated"] is False
    assert usage["usage_id"]


def test_an_in_band_cli_error_fails_without_leaking_into_the_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = json.dumps(
        {"type": "result", "subtype": "error", "is_error": True, "session_id": "s", "result": "rate limit exceeded"}
    )
    result = _execute(get_engine("claude"), monkeypatch, _Completed(envelope, "boom", returncode=1))
    assert result.success is False
    # A Worker that failed must not hand the coordinator an error string as if it
    # were a finding.
    assert "rate limit" not in result.output
    assert "rate limit" in str(result.metadata.get("error"))
    assert result.metadata.get("reason") == "ENGINE_FAILED"


def test_an_empty_reply_is_a_failure_not_a_silent_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _execute(
        get_engine("claude"), monkeypatch, _Completed(json.dumps({"type": "result", "result": ""}))
    )
    assert result.success is False
    assert "without assistant text" in str(result.metadata.get("error"))


def test_an_unparseable_result_is_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _execute(get_engine("claude"), monkeypatch, _Completed("not json at all"))
    assert result.success is False


def test_a_timeout_is_reported_as_worker_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _execute(get_engine("claude"), monkeypatch, asyncio.TimeoutError("slow"))
    assert result.success is False
    assert result.metadata.get("reason") == "WORKER_TIMEOUT"


def test_a_missing_binary_is_reported_as_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _execute(get_engine("claude"), monkeypatch, FileNotFoundError("no claude"))
    assert result.success is False
    assert result.metadata.get("reason") == "FileNotFoundError"


def test_a_missing_workspace_is_reported_before_any_launch() -> None:
    absent = str(Path(tempfile.gettempdir()) / f"absent-{uuid.uuid4().hex}")
    result = asyncio.run(get_engine("claude").execute(_intent(), absent))
    assert result.success is False
    assert result.metadata.get("reason") == "WORKSPACE_NOT_FOUND"


def test_metadata_never_moves_the_error_into_the_output(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _execute(get_engine("claude"), monkeypatch, FileNotFoundError("no claude"))
    assert "no claude" not in result.output
    assert "no claude" in str(result.metadata.get("error"))
