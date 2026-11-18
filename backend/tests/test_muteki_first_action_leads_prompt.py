"""A Coordinator FIRST ACTION must be the first imperative the Worker reads.

Measured 2026-11-08 over the Runs of Challenges that *had* a FIRST ACTION
available: only **5.6%** ran it as command #0.  The cause was prompt position,
not model disobedience -- ``intent_goal`` (which carries the action) was appended
near the end of ``ctx`` at ~char 1226, while the board pointer's "READ IT FIRST"
sat at ~char 424 and the "Attached files ... inspect them FIRST" line at ~char
130.  An agent that obeys the first imperative it reads therefore spent its first
commands orienting, and a bounded Race lane only has a handful of commands.

These tests pin the ordering invariant, not the exact wording, so the renderers
can be reworded without silently restoring the old order.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from muteki.models.solve_graph import Challenge
from muteki.solver.cli_solver import CliSolver


FIRST_ACTION_TEXT = "curl -s 'http://target/login.php' -d 'user=admin&pass=admin'"


def _solver(*, intent_goal: str, board_written: bool = True) -> CliSolver:
    """A CliSolver built without __init__ -- the prompt renderers only touch these
    attributes, and the real constructor needs a live driver/engine."""
    solver = object.__new__(CliSolver)
    solver.challenge = Challenge(
        id="challenge-1",
        name="[SUCTF 2019]Upload Labs 2",
        category="web",
        description="Upload a webshell and read the secret.",
        target="http://target",
    )
    solver.intent_goal = intent_goal
    solver.intent_id_assigned = ""
    solver._intent_id = ""
    solver.shared_graph = None
    solver.kb = False
    solver._staged_files = []
    solver._board_file_written = board_written
    solver.mode = "race"
    return solver


def _coordinator_goal() -> str:
    """The goal Coordinator actually builds when a recipe matched (`_race`)."""
    return (
        "Exploitation lane. FIRST ACTION -- run it as your very first command, "
        f"before any recon or AGENTS.md read: {FIRST_ACTION_TEXT} "
        "Then read ./AGENTS.md for the rest of the step order. If that action "
        "fails, continue with the next recipe step, not a fresh scan."
    )


# ── the detector ────────────────────────────────────────────────────────────


def test_has_first_action_detects_the_coordinator_marker() -> None:
    assert _solver(intent_goal=_coordinator_goal())._has_first_action() is True


@pytest.mark.parametrize(
    "goal",
    [
        "",
        "general exploration",
        "Race reconnaissance lane: fingerprint the stack.",
        "Solve [SUCTF 2019]Upload Labs 2 [WEB]",
    ],
)
def test_has_first_action_is_false_for_ordinary_goals(goal: str) -> None:
    assert _solver(intent_goal=goal)._has_first_action() is False


# ── ordering: the invariant that was violated ───────────────────────────────


def test_first_action_precedes_the_board_pointer_in_the_exec_prompt() -> None:
    prompt = _solver(intent_goal=_coordinator_goal())._build_prompt()

    action_at = prompt.find(FIRST_ACTION_TEXT)
    board_at = prompt.find("Shared team board")

    assert action_at != -1, "the FIRST ACTION never made it into the prompt"
    assert board_at != -1, "the board block disappeared"
    assert action_at < board_at, (
        "the FIRST ACTION must be readable before the board pointer, otherwise the "
        "worker spends command #0 reading the board (measured: 68% did exactly that)"
    )


def test_first_action_precedes_the_attached_files_line() -> None:
    """The attachment line is the other competing imperative; it must not outrank
    an assigned action either."""
    solver = _solver(intent_goal=_coordinator_goal())
    solver._staged_files = ["source.zip"]

    prompt = solver._build_prompt()

    assert prompt.find(FIRST_ACTION_TEXT) < prompt.find("Attached files")


def test_attached_files_hint_drops_its_FIRST_when_an_action_is_assigned() -> None:
    solver = _solver(intent_goal=_coordinator_goal())
    solver._staged_files = ["source.zip"]
    assert "inspect them FIRST" not in solver._build_prompt()


def test_attached_files_hint_keeps_its_FIRST_without_an_action() -> None:
    solver = _solver(intent_goal="")
    solver._staged_files = ["source.zip"]
    assert "inspect them FIRST" in solver._build_prompt()


def test_board_pointer_stops_claiming_priority_when_an_action_is_assigned() -> None:
    prompt = _solver(intent_goal=_coordinator_goal())._build_prompt()

    assert "READ IT FIRST" not in prompt
    assert "Read it AFTER your assigned first command" in prompt


def test_board_pointer_keeps_priority_when_no_action_is_assigned() -> None:
    """Regression guard: without a recipe the board genuinely IS the right first
    move -- demoting it there would make bootstrap workers re-walk old ground."""
    prompt = _solver(intent_goal="")._build_prompt()

    assert "READ IT FIRST" in prompt
    assert "Read it AFTER your assigned first command" not in prompt


def test_the_goal_is_rendered_once_not_twice() -> None:
    """It now leads the prompt, so the trailing "Course correction" copy is
    suppressed -- emitting both re-opens the ambiguity the reorder removes."""
    prompt = _solver(intent_goal=_coordinator_goal())._build_prompt()

    assert prompt.count(FIRST_ACTION_TEXT) == 1
    assert "Course correction" not in prompt


def test_the_trailing_course_correction_still_renders_without_an_action() -> None:
    prompt = _solver(intent_goal="Pivot to the /admin panel behind the WAF.")._build_prompt()

    assert "Course correction" in prompt
    assert "Pivot to the /admin panel behind the WAF." in prompt


# ── the explore renderer has the same contract ──────────────────────────────


def test_explore_prompt_also_leads_with_the_action() -> None:
    prompt = _solver(intent_goal=_coordinator_goal())._build_explore_prompt()

    action_at = prompt.find(FIRST_ACTION_TEXT)
    board_at = prompt.find("Shared team board")

    assert action_at != -1
    assert board_at != -1
    assert action_at < board_at


def test_explore_prompt_without_an_action_is_unchanged() -> None:
    prompt = _solver(intent_goal="general exploration")._build_explore_prompt()

    assert "Do this first — before anything else" not in prompt
    assert "general exploration" in prompt


# ── the probe channel: an unmeasured recipe still has to lead ───────────────


def _probe_goal_text() -> str:
    """What Coordinator builds for a matched-but-unmeasured recipe."""
    from app.solver.muteki.coordinator import _probe_goal

    return _probe_goal("POST /service/extdirect", "cmdi=param→shell")


def test_probe_goal_is_recognised_as_a_concrete_first_move() -> None:
    """A probe leads the prompt for the same reason an order does: it is more
    actionable than a technique class, and it is skipped entirely if it lands
    after the board pointer."""
    assert _solver(intent_goal=_probe_goal_text())._has_first_action() is True


def test_probe_goal_renders_before_the_board_pointer() -> None:
    prompt = _solver(intent_goal=_probe_goal_text())._build_prompt()

    assert prompt.find("POST /service/extdirect") < prompt.find("Shared team board")


def test_probe_is_worded_as_unverified_not_as_an_order() -> None:
    text = _probe_goal_text()

    assert "NOT verified on this instance" in text
    assert "NORMAL outcome" in text
    # It must not borrow the authority language of a flag-proven recipe.
    assert "FIRST ACTION" not in text


def test_probe_keeps_the_technique_fallback_for_after_it_fails() -> None:
    """A failed probe must still leave a decision procedure, not a blank."""
    assert "cmdi=param→shell" in _probe_goal_text()
