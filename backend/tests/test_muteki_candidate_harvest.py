"""A flag-shaped value becomes a candidate the moment it is seen.

The requirement is that a box printing ``n1book{...}`` must not have that value
thrown away just because the challenge's own banner is ``CTF2{...}``: it is
recorded as a candidate, the solve loop keeps hunting for a better value, and if
the clock runs out the candidate is tried -- including re-wrapped under the
challenge's banner.

These lock the two halves:

* harvesting while the Run is still running (so the next Reason turn can see it);
* the value surviving as ``suspected`` rather than being promoted or dropped.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from app.solver.muteki.coordinator import MutekiCoordinator
from app.solver.muteki.graph import MutekiGraph

PATTERN = r"CTF2\{[^}]+\}"


def _coordinator() -> MutekiCoordinator:
    graph = MutekiGraph(
        Path(tempfile.mkdtemp()) / "graph.db", challenge_id="t", flag_pattern=PATTERN
    )
    coordinator = object.__new__(MutekiCoordinator)
    coordinator.graph = graph
    coordinator._stop_reason = None
    coordinator._timed_out = True
    return coordinator


async def test_a_foreign_banner_is_harvested_as_a_candidate() -> None:
    coordinator = _coordinator()
    coordinator.graph.add_fact(
        actor="worker-1",
        content="source decodes to //n1book{afr_1_solved} -- this is the flag",
        evidence_refs=("call_1",),
    )

    await coordinator._harvest_candidates()

    kept = [str(f.flag_value) for f in coordinator.graph.suspected_flags()]
    assert kept == ["n1book{afr_1_solved}"]
    # Harvesting is bookkeeping: it must never promote the value to a solve.
    assert coordinator.graph.flags(verified_only=True) == []


async def test_harvesting_is_idempotent_across_turns() -> None:
    coordinator = _coordinator()
    coordinator.graph.add_fact(
        actor="worker-1",
        content="//n1book{afr_1_solved}",
        evidence_refs=("call_1",),
    )

    await coordinator._harvest_candidates()
    await coordinator._harvest_candidates()
    await coordinator._harvest_candidates()

    assert [str(f.flag_value) for f in coordinator.graph.suspected_flags()] == [
        "n1book{afr_1_solved}"
    ]


async def test_harvest_ignores_ordinary_prose() -> None:
    coordinator = _coordinator()
    coordinator.graph.add_fact(
        actor="worker-1",
        content="response body {css} and window.__data = {}; no token found",
        evidence_refs=("call_1",),
    )

    await coordinator._harvest_candidates()

    assert coordinator.graph.suspected_flags() == []


async def test_candidate_is_offered_under_the_challenge_banner_at_the_end() -> None:
    """The candidate recorded mid-Run is what the Finalize recovery tries."""

    coordinator = _coordinator()
    coordinator.graph.add_fact(
        actor="worker-1",
        content="FOUND_FLAG=n1book{afr_1_solved}",
        evidence_refs=("call_1",),
    )
    coordinator.graph.emit_event(
        actor="worker-1",
        event_type="muteki.worker_command",
        payload={
            "kind": "tool_result",
            "call_id": "call_1",
            "result_excerpt": "<?php\ndie('no no no');\n//n1book{afr_1_solved}",
        },
    )

    await coordinator._harvest_candidates()
    # Mid-Run: a candidate, not a solve.
    assert coordinator.graph.flags(verified_only=True) == []
    assert "n1book{afr_1_solved}" in [
        str(f.flag_value) for f in coordinator.graph.suspected_flags()
    ]

    # End of Run: the value is witnessed, so it is offered under the challenge
    # banner as a submission candidate -- and still not declared a local solve,
    # because only the platform can confirm its own minted flag.
    assert await coordinator._recover_flag_from_evidence("RACE_WORKER_TIMEOUT") is False
    assert coordinator.graph.flags(verified_only=True) == []
    kept = [str(f.flag_value) for f in coordinator.graph.suspected_flags()]
    assert "n1book{afr_1_solved}" in kept
    assert "CTF2{afr_1_solved}" in kept


async def test_a_harvested_candidate_is_offered_without_any_surviving_fact() -> None:
    """The upstream backend drops concluded facts; the candidate must survive it.

    This is the shape production actually reaches: the Worker states the answer
    in a fact that is then concluded, so ``facts()`` no longer returns it by the
    time Finalize runs.  The candidate recorded during the Run is the only
    durable record, and it is what has to be offered to the gate.
    """

    coordinator = _coordinator()
    coordinator.graph.add_fact(
        actor="worker-1",
        content="FOUND_FLAG=n1book{afr_1_solved}",
        evidence_refs=("call_1",),
    )
    # Retained output proves the box printed it.
    coordinator.graph.emit_event(
        actor="worker-1",
        event_type="muteki.worker_command",
        payload={
            "kind": "tool_result",
            "call_id": "call_1",
            "result_excerpt": "<?php\ndie('no no no');\n//n1book{afr_1_solved}",
        },
    )
    await coordinator._harvest_candidates()
    assert "n1book{afr_1_solved}" in [
        str(f.flag_value) for f in coordinator.graph.suspected_flags()
    ]

    # Simulate the fact being concluded: replace the fact reader with nothing,
    # which is what the upstream adapter does for a terminal fact.
    original = coordinator.graph.facts
    coordinator.graph.facts = lambda **kw: []
    try:
        await coordinator._recover_flag_from_evidence("RACE_WORKER_TIMEOUT")
    finally:
        coordinator.graph.facts = original

    # The candidate recorded before the fact vanished is still what gets tried.
    assert "CTF2{afr_1_solved}" in [
        str(f.flag_value) for f in coordinator.graph.suspected_flags()
    ]
