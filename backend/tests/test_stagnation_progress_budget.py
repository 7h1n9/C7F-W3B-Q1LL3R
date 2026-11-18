"""The stagnation allowance grows with demonstrated progress, not with time.

Why a flat 40 is wrong for half the corpus
------------------------------------------
``stagnation_command_limit = 40`` was calibrated on 484 Runs (see
``test_command_stagnation.py``): 40 commands without a new fact fires on 14% of
SOLVED Runs and 34% of UNSOLVED ones.  That calibration is right, and it is
kept -- but it was measured over a population dominated by **single-hop** boxes,
where "40 commands and nothing learned" really does mean the Worker is looping.

Multi-stage boxes are structurally different.  The steps *between* stages are
invisible to the fact counter by construction:

* deriving a filename oracle (``md5(php_rand(time()).'admin')``) takes a script
  and a hundred rejections before it resolves;
* brute-forcing a truncated hash takes 10^6 attempts;
* compiling a shared object that the target's PHP will load takes repeated
  ``struct``-level edits.

None of that produces a ``VERIFIED_FACT=`` until it succeeds, so a flat counter
reads a Worker that is making real progress as a Worker that is stuck.

Measured 2026-11-08 over the 46 UNSOLVED non-CVE Web Challenges on the arena:

* **33 of 46 (72%)** had already produced >=2 real technical conclusions in
  their last Run -- they were not flailing;
* the representative case (``[CISCN2019 总决赛 Day1 Web1]滑稽云音乐``) had leaked
  the admin password, confirmed the firmware upload, and solved the filename
  oracle, then died constructing the ELF object during convergence;
* 20 of the 46 had already burned all three retry attempts.

Meanwhile CVE boxes -- which are 1-2 hops and now solve at 68% -- must not get
slower.  So the rule is: **progress buys runway, and nothing else does.**  A Run
with no facts keeps exactly the flat 40.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.solver.muteki.coordinator import (  # noqa: E402
    CoordinatorConfig,
    MutekiCoordinator,
)


class _Graph:
    """Only the one method the budget reader calls."""

    def __init__(self, fact_count: int):
        self.fact_count = fact_count

    def facts(self):
        return [SimpleNamespace(content=f"fact-{i}") for i in range(self.fact_count)]


class _BrokenGraph:
    def facts(self):
        raise RuntimeError("graph is being rebuilt")


def coordinator(fact_count: int = 0, **overrides):
    """A Coordinator carrying the real config defaults plus a fake graph."""

    c = object.__new__(MutekiCoordinator)
    c.config = CoordinatorConfig(**overrides)
    c.graph = _Graph(fact_count)
    return c


# ── the shape of the rule ───────────────────────────────────────────────────


def test_a_run_with_no_facts_keeps_the_flat_budget() -> None:
    """The fast-fail case, and therefore every CVE Run, is untouched."""
    assert coordinator(0)._stagnation_limit_for_progress() == 40


def test_facts_up_to_the_threshold_still_get_the_flat_budget() -> None:
    """Two facts is the bar, not the trigger.  The bonus is for momentum."""
    assert coordinator(2)._stagnation_limit_for_progress() == 40


def test_progress_above_the_threshold_buys_runway() -> None:
    # 3 facts -> 1 above the threshold -> 40 + 20
    assert coordinator(3)._stagnation_limit_for_progress() == 60
    # 6 facts -> 4 above -> 40 + 80
    assert coordinator(6)._stagnation_limit_for_progress() == 120


def test_the_runway_is_capped() -> None:
    """A Run cannot spin forever by writing cheap facts."""
    assert coordinator(50)._stagnation_limit_for_progress() == 140


def test_the_growth_is_monotonic() -> None:
    limits = [coordinator(n)._stagnation_limit_for_progress() for n in range(0, 25)]
    assert limits == sorted(limits)


# ── it must be switchable off ───────────────────────────────────────────────


def test_a_zero_bonus_restores_the_flat_budget_exactly() -> None:
    c = coordinator(20, stagnation_progress_bonus_per_fact=0)
    assert c._stagnation_limit_for_progress() == 40


def test_a_disabled_base_limit_stays_disabled() -> None:
    """``stagnation_command_limit=0`` means 'no command-level convergence'; the
    bonus must not resurrect it."""
    c = coordinator(20, stagnation_command_limit=0)
    assert c._stagnation_limit_for_progress() == 0


def test_a_ceiling_below_the_base_is_ignored() -> None:
    c = coordinator(20, stagnation_command_limit_max=10)
    assert c._stagnation_limit_for_progress() == 40


# ── defensive reads: this runs on the Worker's callback thread ──────────────


def test_a_graph_that_raises_does_not_break_the_callback_path() -> None:
    """The recorder path must never raise; a graph mid-rebuild is normal."""
    c = object.__new__(MutekiCoordinator)
    c.config = CoordinatorConfig()
    c.graph = _BrokenGraph()
    assert c._stagnation_limit_for_progress() == 40


def test_a_coordinator_without_a_graph_is_safe() -> None:
    """``object.__new__`` test doubles never run ``__init__``."""
    c = object.__new__(MutekiCoordinator)
    c.config = CoordinatorConfig()
    assert c._stagnation_limit_for_progress() == 40


def test_a_partial_config_namespace_keeps_old_behaviour() -> None:
    """Test doubles built with ``SimpleNamespace`` lack the new fields; the
    absent value must mean "no bonus", not "crash"."""
    c = object.__new__(MutekiCoordinator)
    c.config = SimpleNamespace(stagnation_command_limit=40)
    c.graph = _Graph(30)
    assert c._stagnation_limit_for_progress() == 40


# ── the count is memoised ───────────────────────────────────────────────────


def test_the_fact_count_is_not_re_queried_on_every_command() -> None:
    """This is called once per Worker command from another thread."""
    calls: list[int] = []

    class _CountingGraph:
        def facts(self):
            calls.append(1)
            return [SimpleNamespace(content="f")] * 5

    c = object.__new__(MutekiCoordinator)
    c.config = CoordinatorConfig()
    c.graph = _CountingGraph()

    for _ in range(10):
        c._stagnation_limit_for_progress()

    assert len(calls) == 1, "the fact count must be memoised, not re-read per command"


def test_the_memo_expires_so_growth_is_eventually_seen() -> None:
    """A missed invalidation would pin the budget forever; the TTL bounds that."""

    class _GrowingGraph:
        def __init__(self):
            self.count = 0

        def facts(self):
            self.count += 4
            return [SimpleNamespace(content="f")] * self.count

    c = object.__new__(MutekiCoordinator)
    c.config = CoordinatorConfig()
    c.graph = _GrowingGraph()

    first = c._stagnation_limit_for_progress()
    # Force the memo to look stale without sleeping.
    c.__dict__["_stagnation_fact_count_at"] = 0.0
    second = c._stagnation_limit_for_progress()

    assert second > first


# ── end-to-end: the allowance actually reaches the counter ──────────────────


def _feed(c, count: int, excerpt: str = "plain output") -> None:
    for _ in range(count):
        c._note_command_for_stagnation("curl -s http://x/", excerpt)


def _wire(c) -> None:
    c._stop_requested = False
    c._converging = False
    c._commands_since_progress = 0
    c._runtime_loop_ref = None
    c._converge_requests = []
    c._convergence_budget_seconds = lambda: 120.0
    c.request_convergence = lambda reason, budget=None: c._converge_requests.append(
        (reason, budget)
    )


def test_a_progressing_run_survives_past_the_flat_limit() -> None:
    """The whole point: 50 barren commands would converge a 0-fact Run, and must
    not converge a Run that has already produced 6 facts."""
    c = coordinator(6)
    _wire(c)

    _feed(c, 50)
    assert c._converge_requests == [], "6 facts bought 120 commands of runway"

    _feed(c, 71)  # 121 total
    assert len(c._converge_requests) == 1


def test_a_run_with_no_progress_still_converges_at_40() -> None:
    """Regression guard on the original calibration (14% of SOLVED / 34% of
    UNSOLVED).  Those Runs must be stopped exactly as before."""
    c = coordinator(0)
    _wire(c)

    _feed(c, 39)
    assert c._converge_requests == []

    _feed(c, 1)
    assert len(c._converge_requests) == 1


def test_a_zero_threshold_means_every_fact_counts() -> None:
    """``min_facts=0`` is "no threshold", not "clamp to 1".

    A ``max(1, min_facts)`` guard would turn an explicit 0 into a 1-fact
    exemption, which is a different policy than the one configured.
    """
    c = coordinator(4, stagnation_progress_bonus_min_facts=0)
    # 4 facts, threshold 0 -> 4 eligible -> 40 + 80
    assert c._stagnation_limit_for_progress() == 120
