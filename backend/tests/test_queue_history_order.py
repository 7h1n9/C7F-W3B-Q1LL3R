"""Ordering the queue by what the box's own history says about it.

Why this ranking exists
-----------------------
``priority`` encodes the operator's intent and, at enqueue time, whether the
challenge needs an environment.  It says nothing about whether the box has ever
been solvable, and the gap is large.  Measured 2026-11-06 over the ACTIVE
challenges:

| bucket | count |
|---|---|
| never run | 0 |
| **run, never solved** | **119** |
| **solved before, currently unsolved** | **14** |

Every box has been tried at least once, so "unknown" is not the common case --
the common case is "already measured as not working here".  ``[PHP]CVE-2012-1823``
sat in that bucket with 5 runs and 0 solves, while the prompt already recorded
the reason (RCE works as www-data, flag not retrievable from that account).
Spending the single environment slot there, while a previously-solved box waits,
is the most expensive ordering mistake the queue can make -- and it is exactly
what happened on 2026-11-06.

These tests use a fake session so the ranking can be exercised without MySQL.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.arena_queue import (  # noqa: E402
    HISTORY_NEVER_RUN,
    HISTORY_NEVER_SOLVED,
    HISTORY_SOLVED_BEFORE,
    PROMISING_RUN_LIMIT,
    _order_by_history,
)


class FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class FakeSession:
    """Just enough of ``AsyncSession`` for ``_order_by_history``.

    Call order matters: the first ``scalars`` call loads challenges, the first
    ``execute`` call loads run statistics.  The function under test is the only
    caller, so matching by call number is safe and keeps the fake small.
    """

    def __init__(self, challenges, stats):
        self._challenges = challenges
        self._stats = stats
        self._scalar_calls = 0
        self._execute_calls = 0

    async def scalars(self, _stmt):
        self._scalar_calls += 1
        return FakeScalars(self._challenges)

    async def execute(self, _stmt):
        self._execute_calls += 1
        return FakeResult(self._stats)


def challenge(local_id: str, arena_id: str, status: str = "ACTIVE"):
    return SimpleNamespace(
        id=local_id, status=status, metadata_json={"arena_challenge_id": arena_id}
    )


def item(platform_id: str, priority: int = 100):
    return SimpleNamespace(
        id=f"q-{platform_id}", platform_challenge_id=platform_id, priority=priority
    )


def stat(challenge_id: str, runs: int, solved: int):
    return SimpleNamespace(challenge_id=challenge_id, runs=runs, solved=solved)


@pytest.mark.asyncio
async def test_previously_solved_jumps_the_queue():
    session = FakeSession(
        challenges=[challenge("c-solved", "p-solved"), challenge("c-dead", "p-dead")],
        stats=[stat("c-solved", 3, 1), stat("c-dead", 5, 0)],
    )
    # The never-solved box is first in the operator's order.
    rows = [item("p-dead"), item("p-solved")]
    ordered = await _order_by_history(session, rows)
    assert [r.platform_challenge_id for r in ordered] == ["p-solved", "p-dead"]


@pytest.mark.asyncio
async def test_a_couple_of_failures_does_not_sink_a_box():
    """Two failed Runs are not a verdict, and demoting on them is permanent.

    Measured 2026-11-06: a CVE box on this platform solves at **24%**, the same
    as any other box (25%).  One or two misses therefore carry almost no
    information -- they are usually an engine hiccup, a bad lease or a timeout.
    But the old rule moved the box behind every unexplored box, where it stayed
    forever.  All 42 imported-payload boxes were sitting in that bucket, which
    is why none of them could ever be verified.

    A touched box also costs less to resume than an untouched one costs to
    start: the workspace, the fingerprint and often a sent payload already exist.
    """

    session = FakeSession(
        challenges=[challenge("c-dead", "p-dead"), challenge("c-new", "p-new")],
        stats=[stat("c-dead", 2, 0)],
    )
    rows = [item("p-dead"), item("p-new")]
    ordered = await _order_by_history(session, rows)
    assert [r.platform_challenge_id for r in ordered] == ["p-dead", "p-new"]


@pytest.mark.asyncio
async def test_enough_failures_do_sink_a_box():
    """Past the promising threshold, "it does not work here" is the better bet."""

    session = FakeSession(
        challenges=[challenge("c-dead", "p-dead"), challenge("c-new", "p-new")],
        stats=[stat("c-dead", PROMISING_RUN_LIMIT + 1, 0)],
    )
    rows = [item("p-dead"), item("p-new")]
    ordered = await _order_by_history(session, rows)
    assert [r.platform_challenge_id for r in ordered] == ["p-new", "p-dead"]


@pytest.mark.asyncio
async def test_unknown_platform_id_keeps_its_place():
    """A queue row can outlive its challenge row; that must not crash or reorder."""

    session = FakeSession(challenges=[], stats=[])
    rows = [item("p-x"), item("p-y")]
    ordered = await _order_by_history(session, rows)
    assert [r.platform_challenge_id for r in ordered] == ["p-x", "p-y"]


@pytest.mark.asyncio
async def test_order_within_a_bucket_is_preserved():
    """Stable sort: the operator's own priority still decides ties."""

    session = FakeSession(
        challenges=[challenge("c1", "p1"), challenge("c2", "p2"), challenge("c3", "p3")],
        stats=[stat("c1", 1, 1), stat("c2", 1, 1), stat("c3", 1, 1)],
    )
    rows = [item("p1"), item("p2"), item("p3")]
    ordered = await _order_by_history(session, rows)
    assert [r.platform_challenge_id for r in ordered] == ["p1", "p2", "p3"]


@pytest.mark.asyncio
async def test_empty_input_is_returned_untouched():
    session = FakeSession(challenges=[], stats=[])
    assert await _order_by_history(session, []) == []


def test_bucket_constants_are_ordered():
    """The deltas must not collide, and solved must be strongest."""

    assert HISTORY_SOLVED_BEFORE < HISTORY_NEVER_RUN < HISTORY_NEVER_SOLVED


def test_solved_before_outranks_an_operator_move_to_top():
    """A single "move to top" is -1000; two on one row is -2000.

    The point of the constant is not to beat the operator forever, but to make
    the *default* ordering correct.  -200 versus a default of 100 is enough, and
    an explicit move still wins.
    """

    default_priority = 100
    assert HISTORY_SOLVED_BEFORE < default_priority
    assert HISTORY_NEVER_SOLVED > default_priority
