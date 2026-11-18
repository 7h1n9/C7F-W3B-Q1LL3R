"""Claude-cli workers must receive the run's disciplines.

Regression context (measured 2026-11-12 on run ``94af75e5``, Peizheng Group): the
backend writes the run policy, reconnaissance discipline, hints and the matched
recipe into ``AGENTS.md``.  Codex reads that file; Claude Code reads
``CLAUDE.md``.  The image's ``runtime-agent`` seeds ``CLAUDE.md`` with 1825 chars
of generic environment text that never mentions the disciplines, so the
claude-cli worker -- the one that produced 454 KB of the run's actual work --
emitted **0** ``VERIFIED_FACT`` markers while its codex peer emitted 14.  Facts
therefore arrived only at Finalize instead of steering the next turn.

``_sync_claude_md`` mirrors the AGENTS.md body into ``CLAUDE.md``, keeping the
environment summary that would otherwise be lost when this write wins over the
image's seeding (``runtime-agent`` does not overwrite an existing file -- verified
by mounting a pre-written CLAUDE.md into the worker image).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.workspace import (
    CLAUDE_ENV_PREAMBLE,
    CLAUDE_SYNC_MARKER,
    _sync_claude_md,
)

AGENTS_BODY = "# Run Policy\n\nPrint VERIFIED_FACT=<finding> the moment a command proves it.\n"


def test_claude_md_receives_the_agents_body(tmp_path: Path):
    _sync_claude_md(tmp_path, AGENTS_BODY)

    text = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")

    assert AGENTS_BODY.strip() in text
    assert "VERIFIED_FACT" in text


def test_the_environment_summary_is_kept(tmp_path: Path):
    """The image's own CLAUDE.md is unreachable from the backend, so the preamble
    here is the only thing standing between the write and an empty file."""

    _sync_claude_md(tmp_path, AGENTS_BODY)

    text = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")

    assert text.startswith("你在一个 Kali Linux 容器里")
    assert "knowledges/hacktricks" in text
    assert CLAUDE_SYNC_MARKER in text


def test_repeated_sync_does_not_stack_preambles(tmp_path: Path):
    """Workspaces are reused across attempts; a second pass must refresh the body
    rather than append another copy of the preamble."""

    _sync_claude_md(tmp_path, AGENTS_BODY)
    _sync_claude_md(tmp_path, AGENTS_BODY.replace("Run Policy", "Run Policy v2"))

    text = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")

    assert text.count(CLAUDE_SYNC_MARKER) == 1
    assert text.count("你在一个 Kali Linux 容器里") == 1
    assert "Run Policy v2" in text


def test_an_image_seeded_claude_md_is_replaced_not_appended(tmp_path: Path):
    """Simulates the real ordering: the image seeded a generic CLAUDE.md first
    (on a reused workspace), then this write lands.  Leaving both would ship a
    file with no disciplines, which is the bug."""

    (tmp_path / "CLAUDE.md").write_text("# Generic image text\n\nno disciplines here\n", encoding="utf-8")

    _sync_claude_md(tmp_path, AGENTS_BODY)

    text = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")

    assert "no disciplines here" not in text
    assert "VERIFIED_FACT" in text


def test_a_read_only_workspace_does_not_raise(tmp_path: Path, monkeypatch):
    """Workspace creation must never fail because the mirror failed -- the Run can
    still proceed on AGENTS.md alone."""

    def _boom(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", _boom)

    _sync_claude_md(tmp_path, AGENTS_BODY)  # must not raise


def test_the_preamble_stays_short():
    """It exists to stop the Worker assuming a tool is missing, not to duplicate
    the full track matrix; unbounded growth here pushes the disciplines down."""

    assert len(CLAUDE_ENV_PREAMBLE) < 900
