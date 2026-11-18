"""Trimming the execution checklist to the lessons a box can use.

The problem, measured
---------------------
``EXECUTION_CHECKLIST`` grew one lesson at a time, each added after a Run failed
in that particular way.  By 2026-11-06 it was **4734 chars over 7 lessons**, and
four of the seven are only meaningful for a particular kind of target:

| lesson | chars | who needs it |
|---|---|---|
| Flag hunt | 713 | everyone |
| Confirm before pivoting | 119 | everyone |
| Stop condition | 621 | everyone |
| Normal page ≠ failed injection | 424 | PHP-ish |
| PHP entry-point traps | 441 | PHP |
| Command injection output | 747 | cmd-injection |
| Stored-name derivation | 840 | upload/LFI |

A Joomla box was being told to rotate `disable_functions`; a Samba box was told
about `md5($file['name'])`.  Neither is wrong, and neither can be acted on
there.  The cost is not that the model disobeys -- it is that the one line that
mattered is harder to find in 4.7k chars of mostly-irrelevant advice.

What must never regress
-----------------------
The three platform lessons, and the "report as you work" section.  Those are the
ones that apply to every box, and a Run that loses the flag hunt loses the whole
point of the checklist.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.known_exploit import (  # noqa: E402
    EXECUTION_CHECKLIST,
    _checklist_sections,
    _PLATFORM_LESSONS,
    execution_checklist_for,
)

HEADER = "## Code execution checklist (applies to every box)"


def headlines(text: str) -> list[str]:
    return re.findall(r"^- \*\*(.+?)\*\*", text, re.M)


# --- the full text stays intact -------------------------------------------


def test_full_constant_is_unchanged():
    """``EXECUTION_CHECKLIST`` is the superset; other tests index into it."""

    assert len(EXECUTION_CHECKLIST) == 4734
    assert len(headlines(EXECUTION_CHECKLIST)) == 7


def test_sections_split_recovers_every_lesson():
    lessons, trailer = _checklist_sections()
    assert len(lessons) == 7
    assert "Report findings WHILE you work" in trailer


# --- trimming -------------------------------------------------------------


def test_unmatched_box_gets_platform_lessons_only():
    text = execution_checklist_for("[Joomla]CVE-2017-8917", "点击启动靶机。")
    got = headlines(text)
    assert got == list(_PLATFORM_LESSONS) or set(got) == set(_PLATFORM_LESSONS)
    assert len(got) == 3


def test_joomla_does_not_get_the_php_traps():
    """The concrete waste: a Joomla box was told to rotate PHP primitives."""

    text = execution_checklist_for("[Joomla]CVE-2017-8917", "点击启动靶机。")
    assert "disable_functions" not in text
    assert "PHP entry points" not in text


def test_samba_does_not_get_the_upload_derivation():
    """And a Samba box was told about ``md5($file['name'])``."""

    text = execution_checklist_for("[Samba]CVE-2017-7494", "点击启动靶机。")
    assert "md5($file['name'])" not in text


@pytest.mark.parametrize(
    "name,expected",
    [
        ("[PHP]CVE-2012-1823", "PHP entry points"),
        ("[PHP]CVE-2024-4577", "PHP entry points"),
        ("[GXYCTF2019]Ping Ping Ping", "Command injection"),
        ("[0CTF 2016]piapiapia", "stored name or path"),
        ("[Drupal]CVE-2018-7600", "stored name or path"),
    ],
)
def test_matching_box_gets_its_lesson(name: str, expected: str):
    text = execution_checklist_for(name, "点击启动靶机。")
    assert expected in text


def test_trailing_report_section_always_survives():
    """A Run that publishes nothing cannot steer its next turn."""

    for name in ("[Joomla]CVE-2017-8917", "[PHP]CVE-2012-1823", "[Samba]CVE-2017-7494"):
        text = execution_checklist_for(name, "点击启动靶机。")
        assert "Report findings WHILE you work" in text
        assert "VERIFIED_FACT=" in text
        assert "DEADEND=" in text


def test_header_always_present():
    for name in ("", "x", "[Joomla]CVE-2017-8917"):
        assert HEADER in execution_checklist_for(name, "")


def test_trimming_actually_saves_space():
    """An unmatched box must be smaller -- but the *count* of lessons is the point.

    A raw byte ratio understates the change now that a fixed-cost guide (the
    session tool) rides every checklist, so the guard is on what actually
    matters: the four product/technique lessons a plain box cannot use are gone,
    and the three platform lessons survive.
    """

    trimmed = execution_checklist_for("[Joomla]CVE-2017-8917", "")
    got = headlines(trimmed)
    # Fewer lessons than the full text ships.
    assert len(got) < len(headlines(EXECUTION_CHECKLIST))
    # And specifically, the irrelevant ones are absent.
    assert "PHP entry points" not in trimmed
    assert "md5($file['name'])" not in trimmed
    # Still smaller overall, even with the fixed-cost guides.
    assert len(trimmed) < len(EXECUTION_CHECKLIST) * 0.75


def test_technique_argument_can_select_a_lesson():
    """``category`` is used when the name says nothing."""

    text = execution_checklist_for("some-box", "no hint here", "command injection")
    assert "Command injection" in text


def test_empty_input_is_safe():
    text = execution_checklist_for()
    assert HEADER in text
    assert set(headlines(text)) == set(_PLATFORM_LESSONS)


def test_stop_condition_survives_everywhere():
    """The one lesson that prevents the 397-command Joomla run."""

    for name in ("[Joomla]CVE-2017-8917", "[PHP]CVE-2012-1823", "unknown-box"):
        text = execution_checklist_for(name, "")
        assert "Stop condition (do not loop)" in text
        assert "does not expose a retrievable flag" in text


# --- the session tool guide -----------------------------------------------


def test_session_guide_ships_with_every_checklist():
    """Interactive targets are not tied to a product, so this is platform-level."""

    for name in ("[Joomla]CVE-2017-8917", "[PHP]CVE-2012-1823", "unknown-box", ""):
        text = execution_checklist_for(name, "")
        assert "mkt-session" in text, f"missing for {name!r}"


def test_session_guide_names_the_three_commands():
    text = execution_checklist_for("[Joomla]CVE-2017-8917", "")
    assert "mkt-session start" in text
    assert "mkt-session send" in text
    assert "mkt-session close" in text


def test_session_guide_says_when_not_to_use_it():
    """Guard against the guide becoming "always use a session".

    A single request/response is exactly what a pipe does well; pushing the
    session everywhere would add a daemon per interaction for no gain.
    """

    text = execution_checklist_for("x", "")
    assert "pipe is still right" in text.lower() or "A pipe is still right" in text
