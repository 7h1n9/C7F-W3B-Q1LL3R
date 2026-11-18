"""Redaction contract for Worker command/result observability.

The trace has to be usable for post-mortems *and* safe to persist.  These tests
pin both halves: secrets never survive, and the parts a reviewer needs (the
command shape, the flag itself) always do.
"""

from __future__ import annotations

from app.solver.muteki.redaction import COMMAND_LIMIT, RESULT_LIMIT, redact


def test_redacts_header_and_assignment_secrets() -> None:
    text = (
        "curl -H 'Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig' "
        "-H 'Cookie: PHPSESSID=deadbeefcafe' "
        "'http://t/login?password=hunter2&user=admin'"
    )
    out = redact(text)
    assert "eyJhbGciOiJIUzI1NiJ9" not in out
    assert "hunter2" not in out
    assert "deadbeefcafe" not in out
    # Non-secret structure survives, so the command is still replayable.
    assert out.startswith("curl -H 'Authorization:")
    assert "user=admin" in out


def test_redacts_url_userinfo_only() -> None:
    out = redact("http://admin:s3cr3t@10.0.0.5:8080/path?x=1")
    assert "s3cr3t" not in out
    assert out == "http://admin:[redacted]@10.0.0.5:8080/path?x=1"


def test_flag_is_never_masked() -> None:
    """A flag in tool output is the most valuable thing in the trace."""

    out = redact("FLAG=CTF2{this_is_the_actual_flag} and token=abc123")
    assert "CTF2{this_is_the_actual_flag}" in out
    assert "abc123" not in out


def test_bounds_command_and_result_independently() -> None:
    out = redact("A" * (COMMAND_LIMIT + 50))
    assert len(out) > COMMAND_LIMIT  # truncation marker adds text
    assert out.endswith("chars]")

    short = redact("B" * 10, limit=RESULT_LIMIT)
    assert short == "B" * 10


def test_handles_non_string_and_empty() -> None:
    assert redact(None) == ""
    assert redact("") == ""
    assert redact(42) == "42"


def test_already_redacted_marker_is_idempotent() -> None:
    once = redact("password=hunter2")
    twice = redact(once)
    assert once == twice
    assert once.count("[redacted]") == 1
