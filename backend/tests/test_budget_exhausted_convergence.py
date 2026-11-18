"""When no Worker round can still fit, the Coordinator must stop deciding.

Measured on run ``780fc60b`` (Weblogic CVE-2018-2628): the Worker turn timed out
at 03:04:23, the Coordinator still issued a full Reason call, and it came back at
03:05:17 -- 54 seconds of planning whose Intents no Worker could ever execute,
because ``_a_round_still_fits()`` was already false.  The Run then claimed and
immediately concluded that unexecutable Intent.

So the timeout path produced exactly the two things it is supposed to prevent:

* repeated processing -- a whole Reason pass spent on work that cannot start;
* inconsistent state -- a live Intent claimed at the end of a Run with no
  Worker behind it.

The contract asserted below: with zero budget left and nothing in flight, the
loop stops and hands the Board to Finalize, which owns claim-then-conclude for
everything it inherits.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

BACKEND_ROOT = str(Path(__file__).resolve().parents[1])
if BACKEND_ROOT not in sys.path:
    sys.path.append(BACKEND_ROOT)

from app.solver.muteki import (  # noqa: E402
    CoordinatorConfig,
    EngineProfile,
    EventType,
    MutekiCoordinator,
    MutekiGraph,
    MutekiReason,
    MutekiWorkerPool,
    MutekiWorkerPool as _Pool,  # alias keeps the import list readable
)
from app.solver.muteki.adapter.upstream_runtime_graph import UpstreamRuntimeGraph  # noqa: E402


class _CountingReason(MutekiReason):
    """A Reason provider that records every planning call it is asked to make."""

    def __init__(self) -> None:
        self.calls = 0

    async def reason(self, graph):  # noqa: D401 - mirrors the real signature
        self.calls += 1
        return await super().reason(graph)


def _coordinator(tmp_path, reason, *, seconds_left: float):
    graph = MutekiGraph(tmp_path / "budget.sqlite", challenge_id="challenge-budget")

    async def runner(_job):
        return None

    pool = MutekiWorkerPool(graph, runner, max_workers=1)
    coordinator = MutekiCoordinator(
        graph,
        reason,
        pool,
        [EngineProfile("only-engine")],
        config=CoordinatorConfig(
            race_enabled=False,
            interval_seconds=0.0,
            worker_timeout_seconds=1,
            max_idle_polls=1,
        ),
    )
    # Drive the budget directly: the question is what the loop does when the
    # answer to "does another round fit" is already no.
    coordinator._seconds_left = lambda: seconds_left  # type: ignore[assignment]
    return graph, coordinator


def test_no_reason_call_when_the_budget_is_already_gone(tmp_path) -> None:
    reason = _CountingReason()
    graph, coordinator = _coordinator(tmp_path, reason, seconds_left=0.0)

    asyncio.run(coordinator.run(max_ticks=5))

    # Falsifiable: without the guard the loop spends a Reason call per tick and
    # still cannot dispatch anything.
    assert reason.calls == 0, f"Reason was called {reason.calls} times with no budget left"

    descriptions = {
        str(item.payload.get("description") or item.payload.get("reason") or "")
        for item in graph.events_since()
        if item.event_type == EventType.DEAD_END
    }
    assert any("ROUND_BUDGET_EXHAUSTED_BEFORE_REASON" in text for text in descriptions), descriptions


def test_the_run_still_reaches_exactly_one_terminal_event(tmp_path) -> None:
    """Skipping Reason must not skip the handoff."""

    reason = _CountingReason()
    graph, coordinator = _coordinator(tmp_path, reason, seconds_left=0.0)

    asyncio.run(coordinator.run(max_ticks=5))

    finished = [
        item for item in graph.events_since() if item.event_type == EventType.RUN_FINISHED
    ]
    assert len(finished) == 1
    phases = [
        str(item.payload.get("phase"))
        for item in graph.events_since()
        if item.event_type == EventType.PHASE_CHANGED
    ]
    assert phases[-1] == "finalize"


def test_no_intent_is_left_claimed_without_a_worker(tmp_path) -> None:
    """The inconsistency observed on the live Run: claim, then nothing.

    Finalize claims-then-concludes, so every direction it inherits must end
    terminal rather than sitting ``claimed`` with no Worker behind it.
    """

    reason = _CountingReason()
    graph, coordinator = _coordinator(tmp_path, reason, seconds_left=0.0)

    asyncio.run(coordinator.run(max_ticks=5))

    leftover = [
        intent
        for intent in graph.intents()
        if str(getattr(intent, "status", "") or "").casefold() in {"claimed", "open"}
    ]
    assert leftover == [], f"{len(leftover)} direction(s) left non-terminal"


def test_healthy_budget_still_plans(tmp_path) -> None:
    """The guard must not fire when a round genuinely fits."""

    reason = _CountingReason()
    graph, coordinator = _coordinator(tmp_path, reason, seconds_left=600.0)

    asyncio.run(coordinator.run(max_ticks=1))

    assert reason.calls >= 1, "a Run with budget left must still consult Reason"
