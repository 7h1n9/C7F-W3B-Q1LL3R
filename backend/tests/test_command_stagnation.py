"""Command-level stagnation: converging a Worker that is going in circles.

The gap this fills
------------------
Two existing limits were supposed to catch a stuck Worker and neither can see
the failure.  Measured 2026-11-06 on three runs:

| run | commands | worker rounds | status |
|---|---|---|---|
| ``6712279f`` Joomla | 703 | **2** | UNSOLVED |
| ``1b2a8586`` PHP | 74 | **2** | UNSOLVED |
| ``e92b8431`` Drupal | 164 | **2** | SOLVED |

* ``barren_limit = 8`` counts **rounds**.  Every run used two, so the limit was
  never remotely in play no matter how many commands were wasted.
* ``worker_max_turns = 80`` bounds *turns*, and one turn can issue many commands.
* ``no_progress_timeout`` is wall-clock and defaults to disabled.

So the counter has to be **commands since the last new fact**, and it has to act
by *converging* rather than stopping: the in-flight round keeps its chance to
write down what it learned, which is the entire reason convergence exists as a
separate mechanism from the hard stop.

Threshold calibration (max run of commands with no new fact, over 484 runs):

| threshold | fires on SOLVED | fires on UNSOLVED |
|---|---|---|
| 30 | 21% | 40% |
| **40** | **14%** | **34%** |
| 60 | 8% | 25% |

40 gives a 2.4x preference and still leaves 86% of successful runs untouched.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.solver.muteki.coordinator import CoordinatorConfig, MutekiCoordinator  # noqa: E402


def coordinator(limit: int = 40, converging: bool = False, stopping: bool = False):
    """A Coordinator stripped to what the stagnation path touches.

    Built with ``object.__new__`` on purpose: the real ``__init__`` wires a
    pool, a graph and a Worker adapter, none of which this code path reads.  The
    class-level defaults exist so this construction works.
    """

    c = object.__new__(MutekiCoordinator)
    c.config = SimpleNamespace(
        stagnation_command_limit=limit,
        barren_limit=8,
        worker_timeout_seconds=60.0,
        worker_wait_grace_seconds=5.0,
    )
    c._stop_requested = stopping
    c._converging = converging
    c._commands_since_progress = 0
    c._runtime_loop_ref = None
    c._converge_requests = []
    c._convergence_budget_seconds = lambda: 120.0
    c.request_convergence = lambda reason, budget=None: c._converge_requests.append(
        (reason, budget)
    )
    return c


def feed(c, count: int, excerpt: str = "plain output, nothing learned") -> None:
    for _ in range(count):
        c._note_command_for_stagnation("curl -s http://x/", excerpt)


# --- the counter ----------------------------------------------------------


def test_converges_after_the_limit():
    c = coordinator(limit=3)
    feed(c, 2)
    assert c._converge_requests == []
    feed(c, 1)
    assert len(c._converge_requests) == 1
    assert c._converge_requests[0][0].startswith("STAGNATION:")


def test_a_new_fact_resets_the_counter():
    """A run that keeps learning must never be converged by this rule."""

    c = coordinator(limit=3)
    feed(c, 2)
    c._note_command_for_stagnation("echo ok", "VERIFIED_FACT=id is www-data")
    feed(c, 2)
    assert c._converge_requests == []


@pytest.mark.parametrize(
    "marker",
    ["VERIFIED_FACT=x", "DEADEND=no lfi", "got CTF2{abc}", "flag{abc}", "CTF{abc}"],
)
def test_every_progress_marker_resets(marker: str):
    c = coordinator(limit=2)
    feed(c, 1)
    c._note_command_for_stagnation("cmd", marker)
    feed(c, 1)
    assert c._converge_requests == []


def test_disabled_by_zero():
    c = coordinator(limit=0)
    feed(c, 500)
    assert c._converge_requests == []


def test_does_not_converge_when_already_converging():
    c = coordinator(limit=1, converging=True)
    feed(c, 5)
    assert c._converge_requests == []


def test_does_not_converge_when_stopping():
    c = coordinator(limit=1, stopping=True)
    feed(c, 5)
    assert c._converge_requests == []


def test_counter_resets_after_firing():
    """Otherwise every later command would re-request convergence."""

    c = coordinator(limit=2)
    feed(c, 2)
    assert len(c._converge_requests) == 1
    assert c._commands_since_progress == 0
    feed(c, 1)
    assert len(c._converge_requests) == 1


# --- the wiring -----------------------------------------------------------


def test_step_callback_calls_the_stagnation_hook():
    """``_record_worker_step`` is the only place commands arrive from."""

    import inspect

    source = inspect.getsource(MutekiCoordinator._record_worker_step)
    assert "_note_command_for_stagnation" in source


def test_lock_is_per_instance_not_class_level():
    """A shared ``threading.Lock`` would serialise unrelated Coordinators."""

    a = coordinator()
    b = coordinator()
    assert a._stagnation_lock is not b._stagnation_lock
    assert isinstance(a._stagnation_lock, type(threading.Lock()))


def test_lock_survives_object_new():
    """Tests build Coordinators without ``__init__``; the lock must still exist."""

    c = object.__new__(MutekiCoordinator)
    lock = c._stagnation_lock
    assert lock is not None
    assert c._stagnation_lock is lock


def test_default_threshold_is_the_calibrated_one():
    assert CoordinatorConfig().stagnation_command_limit == 40


def test_threshold_does_not_disturb_the_other_limits():
    config = CoordinatorConfig()
    # The round-level and wall-clock limits stay as they were: this is an
    # additional guard, not a replacement.
    assert config.barren_limit == 8
    assert config.no_progress_timeout == 0.0


# --- dead-end shrinks the budget -----------------------------------------


def test_deadend_marks_the_run_and_tightens_the_budget():
    """A stated dead-end is a commitment; the slack after it must be smaller.

    Measured on ``1b2a8586``: dead-end written at command 26 of 36, then ten
    more commands re-running the same ``find`` with a longer ``--max-time``.
    Prose alone did not stop it (the prompt already had a "Stop condition"
    paragraph), so the counter has to.
    """

    c = coordinator(limit=40)
    c.config.stagnation_after_deadend_limit = 5
    feed(c, 3)
    assert c._deadend_seen is False
    assert c._converge_requests == []

    # The dead-end itself is progress, but it also arms the tighter budget.
    c._note_command_for_stagnation("mark-deadend", "DEADEND=flag not retrievable")
    assert c._deadend_seen is True
    assert c._commands_since_progress == 0

    feed(c, 4)
    assert c._converge_requests == [], "still inside the tighter budget"
    feed(c, 1)
    assert len(c._converge_requests) == 1, "tighter budget should have fired"


def test_a_new_fact_after_a_deadend_still_resets():
    """A dead-end closes one direction, not the whole Run."""

    c = coordinator(limit=40)
    c.config.stagnation_after_deadend_limit = 3
    c._note_command_for_stagnation("x", "DEADEND=nope")
    feed(c, 2)
    c._note_command_for_stagnation("y", "VERIFIED_FACT=new path works")
    feed(c, 2)
    assert c._converge_requests == []


def test_zero_after_deadend_keeps_one_flat_budget():
    c = coordinator(limit=5)
    c.config.stagnation_after_deadend_limit = 0
    c._note_command_for_stagnation("x", "DEADEND=nope")
    feed(c, 4)
    assert c._converge_requests == []
    feed(c, 1)
    assert len(c._converge_requests) == 1


def test_after_deadend_budget_never_exceeds_the_base():
    """``min`` guard: a mis-set larger value must not loosen the limit."""

    assert CoordinatorConfig().stagnation_after_deadend_limit <= CoordinatorConfig().stagnation_command_limit


# ── the marker list must not be resettable by prose (2026-11-09) ────────────


def test_the_word_flag_alone_does_not_reset_the_counter():
    """A Worker hunting a flag prints the word constantly.

    Measured on run ``a6251f12``: 17 of 154 tool results contained ``flag`` --
    ``find / -name '*flag*'``, ``grep -r flag /var/www`` -- and each one reset the
    stagnation counter, so the Run reached 296 commands with zero facts.  That is
    precisely the loop this counter exists to stop.

    Only a marker that carries a *value* counts: ``FLAG=`` or a literal flag body.
    """
    c = coordinator(limit=3)
    for _ in range(3):
        c._note_command_for_stagnation(
            "find / -name '*flag*'", "find: '/proc/1/flag': no such file"
        )
    assert len(c._converge_requests) == 1


def test_a_real_flag_marker_still_resets_the_counter():
    """The fix must not disarm the marker that means a flag was actually found."""
    c = coordinator(limit=3)
    feed(c, 2)
    c._note_command_for_stagnation("curl -s http://x/", "FLAG=CTF2{abc}")
    assert c._commands_since_progress == 0
    assert c._converge_requests == []


def test_a_literal_flag_body_still_resets_the_counter():
    c = coordinator(limit=3)
    feed(c, 2)
    c._note_command_for_stagnation("curl -s http://x/", "here: CTF2{deadbeef}")
    assert c._commands_since_progress == 0


# ── repeated commands (2026-11-09) ──────────────────────────────────────────
#
# The fact counter cannot see a Worker that re-sends the same request: every
# probe that returns anything writes a VERIFIED_FACT, which resets the counter.
# Measured on ``[BUUCTF 2018]Online Tool`` (run af47e4d8): 264 commands at one
# target, 131 of them (~50%) sharing their first 60 characters -- one curl
# prefix 70 times -- and the Run still ended UNSOLVED.


def coordinator_with_repeat(repeat_limit: int = 5, limit: int = 40):
    c = coordinator(limit=limit)
    c.config.stagnation_repeat_limit = repeat_limit
    c.config.stagnation_repeat_fingerprint_chars = 60
    return c


def test_the_same_command_five_times_converges():
    c = coordinator_with_repeat()
    cmd = (
        'curl -sS "http://790d9e5a964f503358914ed6.http-ctf2.dasctf.com:80/?host=x"'
        " -o /dev/null"
    )
    feed(c, 4)  # counter at 4, below every other budget
    for _ in range(5):
        c._note_command_for_stagnation(cmd, "ok")
    assert len(c._converge_requests) == 1
    assert str(c._converge_requests[0][0]).startswith("STAGNATION:REPEAT")


def test_a_repeated_command_fires_even_though_output_looks_like_progress():
    """The exact failure: each probe writes a fact-shaped marker, resetting the
    other counter.  The repeat counter must not be reset by that."""
    c = coordinator_with_repeat()
    cmd = 'curl -sS "http://target/x?a=1" -o /tmp/o && head /tmp/o'
    for _ in range(5):
        c._note_command_for_stagnation(cmd, "VERIFIED_FACT=the host parameter is echoed")
    assert any(str(r[0]).startswith("STAGNATION:REPEAT") for r in c._converge_requests)


def test_varying_a_parameter_is_not_a_repeat():
    """Legitimate probing varies a parameter across a small set."""
    c = coordinator_with_repeat()
    for i in range(10):
        c._note_command_for_stagnation(
            f'curl -sS "http://target/?id={i}abcdefghijklmnopqrstuvwxyz"', "ok"
        )
    assert c._converge_requests == []


def test_tiny_housekeeping_commands_cannot_trip_the_repeat_counter():
    """``ls`` repeated is not a loop worth converging on."""
    c = coordinator_with_repeat()
    for _ in range(10):
        c._note_command_for_stagnation("ls", "a.txt b.txt")
    assert c._converge_requests == []


def test_a_zero_repeat_limit_disables_the_check():
    c = coordinator_with_repeat(repeat_limit=0)
    cmd = 'curl -sS "http://target/same-command-every-time" -o /dev/null'
    for _ in range(10):
        c._note_command_for_stagnation(cmd, "ok")
    assert c._converge_requests == []


def test_alternating_two_commands_is_not_a_repeat():
    """A-B-A-B is not the degenerate case; only consecutive identity is."""
    c = coordinator_with_repeat()
    a = 'curl -sS "http://target/a-aaaaaaaaaaaaaaaaaaaaaaaa" -o /dev/null'
    b = 'curl -sS "http://target/b-bbbbbbbbbbbbbbbbbbbbbbbb" -o /dev/null'
    for _ in range(10):
        c._note_command_for_stagnation(a, "ok")
        c._note_command_for_stagnation(b, "ok")
    assert c._converge_requests == []
