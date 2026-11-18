"""The FIRST ACTION must LEAD the lane goal, and its injection must be recorded.

Measured on this platform 2026-11-07 (``docs/notes/2026-11/2026-11-08-fa-adoption-diagnosis.md``):

* Of the 178 Runs of Challenges that had a FIRST ACTION available and dispatched
  a Worker, **zero** issued it as command #0.  **68%** opened with an orientation
  step instead -- 27% read the blackboard skill, 21% read ``./AGENTS.md``, 18%
  listed the workspace -- and only 30% opened by touching the target.
* The old goal text put the ``./AGENTS.md`` pointer two clauses AHEAD of the
  command, so an agent that follows instructions in order spends its first
  command on the read.
* 87 of the 347 Runs of such Challenges never dispatched a Race Worker at all,
  which means the FIRST ACTION was never in their prompt.  That case is invisible
  in ``run_events`` unless injection is recorded -- hence ``EXPLOIT_FIRST_ACTION``.

The tests here pin the wording order, the 600-char window, and the event.
"""

from __future__ import annotations

import asyncio

from app.solver.muteki import (
    CoordinatorConfig,
    EngineProfile,
    EventType,
    MutekiCoordinator,
    MutekiGraph,
    MutekiReason,
    MutekiWorkerPool,
)
from app.solver.muteki.coordinator import _BACKFILL_GOAL_PREFIX
from app.services.known_exploit import _STOP_LOSS_GATE

#: What ``cli_solver`` actually renders: ``intent_goal[:600]``, where
#: ``intent_goal`` is the goal plus this contract suffix (``official_worker``).
_CONTRACT_SUFFIX = "\nStay within this action contract and do not switch tool domains: "
_VISIBLE = 600 - len(_CONTRACT_SUFFIX)


def _run_race(tmp_path, *, first_action: str, gate: str = "", engines: int = 1):
    """Dispatch one Race round and return (goals, events)."""

    graph = MutekiGraph(tmp_path / "g.db", challenge_id="challenge-fa")
    goals: list[str] = []

    async def runner(job):
        if job.role == "race":
            goals.append(job.goal)
        return None

    async def scenario() -> None:
        pool = MutekiWorkerPool(graph, runner, max_workers=engines)
        coordinator = MutekiCoordinator(
            graph,
            MutekiReason(),
            pool,
            [EngineProfile(f"engine-{index}") for index in range(engines)],
            config=CoordinatorConfig(
                interval_seconds=0.0,
                max_workers=engines,
                worker_timeout_seconds=1,
                worker_wait_grace_seconds=0.0,
                **({"exploit_first_action": first_action} if first_action else {}),
                **({"exploit_stop_loss_gate": gate} if gate else {}),
            ),
        )
        await coordinator.run(max_ticks=0)

    asyncio.run(scenario())
    return goals, graph.events_since()


def test_the_first_action_leads_the_goal_before_any_file_pointer(tmp_path) -> None:
    """The command comes before the read.

    An agent follows instructions in order.  Putting "the recipe is in
    ./AGENTS.md" ahead of "FIRST ACTION: <cmd>" is what produced the measured
    orientation-first opening in 68% of Runs.
    """

    goals, _events = _run_race(tmp_path, first_action="GET /uddiexplorer/ and poll for the form")

    assert goals
    goal = goals[0]
    assert goal.index("FIRST ACTION") < goal.index("AGENTS.md"), goal


def test_the_first_action_leads_the_race_lane_goal_too(tmp_path) -> None:
    """The multi-engine path builds its own wording; it must agree."""

    goals, _events = _run_race(
        tmp_path, first_action="POST /ws_utc/config.do", engines=3
    )

    exploit = [g for g in goals if "FIRST ACTION" in g]
    assert exploit, goals
    for goal in exploit:
        assert goal.index("FIRST ACTION") < goal.index("AGENTS.md"), goal


def test_the_goal_says_to_run_it_before_reconnaissance(tmp_path) -> None:
    """``before any recon`` is the operative phrase; without it the slot is advice."""

    goals, _events = _run_race(tmp_path, first_action="GET /x.jsp")
    goal = goals[0]

    assert "very first command" in goal
    assert "before any recon" in goal


def test_the_first_action_survives_the_600_char_window(tmp_path) -> None:
    """``intent_goal[:600]`` cuts from the tail, so a long prefix can erase the slot.

    Measured: the worst realistic configuration (stop-loss gate + backfill
    prefix + a 190-char action) used to put the action entirely outside the
    window.  Leading with it is what keeps it inside.
    """

    action = "PUT /x.jsp/ with a JSP that reads /flag.txt " + "y" * 140
    goals, _events = _run_race(
        tmp_path, first_action=action, gate=_STOP_LOSS_GATE.strip()
    )
    goal = goals[0]

    end = goal.index(action) + len(action)
    assert end <= _VISIBLE, (end, _VISIBLE, goal[:200])


def test_the_backfill_prefix_still_leads_when_it_fires(tmp_path) -> None:
    """The backfill note must stay first: it is what tells the Worker it owns two lanes.

    Asserted on the composition rather than through a live health probe.  The
    prefix is prepended to the goal in ``_race``, and ``intent_goal`` is cut from
    the tail -- so the ordering has to be prefix, then action, then the
    ``AGENTS.md`` pointer.  A probe-driven test would be testing the health-check
    machinery instead of this ordering.
    """

    goals, _events = _run_race(tmp_path, first_action="GET /known-path")
    goal = goals[0]
    prefix = _BACKFILL_GOAL_PREFIX.format(missing=1)

    composed = prefix + goal
    assert composed.index("FIRST ACTION") < composed.index("AGENTS.md")
    # And the action is still inside the 600-char window with the prefix on.
    action = "GET /known-path"
    assert composed.index(action) + len(action) <= _VISIBLE


def test_the_injection_is_recorded_so_adoption_can_be_audited(tmp_path) -> None:
    """Race proposes no Intent, so its goal is persisted nowhere else.

    Without this event ``run_events`` cannot distinguish "the Worker ignored the
    FIRST ACTION" from "the FIRST ACTION was never in the prompt" -- and 87 of
    347 Runs were in the second case.
    """

    goals, events = _run_race(tmp_path, first_action="GET /uddiexplorer/ and poll")

    recorded = [e for e in events if e.event_type == EventType.EXPLOIT_FIRST_ACTION]
    assert len(recorded) == 1
    payload = recorded[0].payload

    assert payload["text"] == "GET /uddiexplorer/ and poll"
    assert payload["lane"] == "exploit:known-path"
    assert payload["chars"] == len("GET /uddiexplorer/ and poll")
    assert payload["in_window"] is True
    assert payload["goal_chars"] > 0
    # The head must show the action leading, which is the whole point.
    assert payload["goal_head"].index("FIRST ACTION") < payload["goal_head"].index("AGENTS.md")


def test_no_event_is_recorded_when_no_action_applies(tmp_path) -> None:
    """A box with no recipe leaves the lane byte-identical to before."""

    _goals, events = _run_race(tmp_path, first_action="")

    assert not [e for e in events if e.event_type == EventType.EXPLOIT_FIRST_ACTION]


def test_the_event_carries_the_recipe_provenance(tmp_path) -> None:
    """``source`` and ``verdict`` are what make the slot auditable later."""

    graph = MutekiGraph(tmp_path / "g3.db", challenge_id="challenge-fa-prov")
    goals: list[str] = []

    async def runner(job):
        if job.role == "race":
            goals.append(job.goal)
        return None

    async def scenario() -> None:
        pool = MutekiWorkerPool(graph, runner, max_workers=1)
        coordinator = MutekiCoordinator(
            graph,
            MutekiReason(),
            pool,
            [EngineProfile("e")],
            config=CoordinatorConfig(
                interval_seconds=0.0,
                max_workers=1,
                worker_timeout_seconds=1,
                worker_wait_grace_seconds=0.0,
                exploit_first_action="GET /x",
                exploit_first_action_source="cve-2018-12613-phpmyadmin-lfi:step0",
                exploit_first_action_verdict="step:command",
            ),
        )
        await coordinator.run(max_ticks=0)

    asyncio.run(scenario())

    recorded = [e for e in graph.events_since() if e.event_type == EventType.EXPLOIT_FIRST_ACTION]
    assert recorded
    assert recorded[0].payload["source"] == "cve-2018-12613-phpmyadmin-lfi:step0"
    assert recorded[0].payload["verdict"] == "step:command"
