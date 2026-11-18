"""Exercise ``_recover_flag_from_evidence`` end to end on a real graph.

Simulates the shape the real-class Runs produced: a fact carrying
``FOUND_FLAG=<box banner>{value}``, the value present in retained tool output,
and no verified flag.  Checks both sides:

* a witnessed value is promoted under the challenge banner;
* an unwitnessed one is refused -- but still retained for review, because the
  value is genuine even when it cannot be accepted.
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


def _retain_output(graph: MutekiGraph, text: str, *, call_id: str = "call_1") -> None:
    """Record a retained tool output, the way a real Worker turn does."""

    graph.emit_event(
        actor="worker-1",
        event_type="muteki.worker_command",
        payload={"kind": "tool_result", "call_id": call_id, "result_excerpt": text},
    )


async def test_recovery_offers_the_value_without_declaring_a_solve() -> None:
    """A re-wrapped value is a submission candidate, never a local solve.

    Run ``de9ab053`` proved why: the local gate accepted ``CTF2{afr_1_solved}``,
    the Run closed COMPLETED_SOLVED, and the platform answered ``rejected`` --
    the arena mints its own per-instance flag.  A local "solved" the arena does
    not honour is worse than an honest UNSOLVED.
    """

    coordinator = _coordinator()
    _retain_output(coordinator.graph, "no no no //n1book{afr_1_solved}")
    coordinator.graph.add_fact(
        actor="worker-1",
        content="FOUND_FLAG=n1book{afr_1_solved}",
        evidence_refs=("call_1",),
    )
    assert await coordinator._recover_flag_from_evidence("RACE_WORKER_TIMEOUT") is False
    # No verified flag was fabricated ...
    assert coordinator.graph.flags(verified_only=True) == []
    # ... but the candidate exists, in both spellings, for the submit path.
    kept = [str(f.flag_value) for f in coordinator.graph.suspected_flags()]
    assert "CTF2{afr_1_solved}" in kept


async def test_recovery_refuses_a_value_the_target_never_printed() -> None:
    coordinator = _coordinator()
    _retain_output(coordinator.graph, "uid=0(root) gid=0(root)")
    coordinator.graph.add_fact(
        actor="worker-1",
        content="FOUND_FLAG=n1book{afr_1_solved}",
        evidence_refs=("call_1",),
    )
    assert await coordinator._recover_flag_from_evidence("RACE_WORKER_TIMEOUT") is False
    assert coordinator.graph.flags(verified_only=True) == []


async def test_a_refused_value_is_still_kept_for_review() -> None:
    """The value is genuine even when its banner is not; do not throw it away.

    This is the shape the real-class boxes reached: the box printed its own
    ``n1book{...}`` banner, so the gate refuses it, but the inner value is the
    answer.  Dropping it loses the only reusable observation.
    """

    coordinator = _coordinator()
    _retain_output(coordinator.graph, "<?php die('no no no'); //n1book{afr_1_solved}")
    coordinator.graph.add_fact(
        actor="worker-1",
        content="FOUND_FLAG=n1book{afr_1_solved}",
        evidence_refs=("call_1",),
    )
    # Force a refusal by making the declared banner something the value cannot
    # satisfy: the pattern requires 32 hex chars.
    coordinator.graph.gate.flag_pattern = r"CTF2\{[0-9a-f]{32}\}"
    coordinator.graph.gate._pattern = __import__("re").compile(r"CTF2\{[0-9a-f]{32}\}")

    assert await coordinator._recover_flag_from_evidence("RACE_WORKER_TIMEOUT") is False
    assert coordinator.graph.flags(verified_only=True) == []
    kept = [str(f.flag_value) for f in coordinator.graph.suspected_flags()]
    assert "CTF2{afr_1_solved}" in kept


async def test_recovery_refuses_when_nothing_was_retained() -> None:
    """A claim supported only by its own prose is not evidence."""

    coordinator = _coordinator()
    coordinator.graph.add_fact(
        actor="worker-1",
        content="FOUND_FLAG=n1book{afr_1_solved}",
        evidence_refs=("b5e147dd2ff5",),
    )
    assert await coordinator._recover_flag_from_evidence("RACE_WORKER_TIMEOUT") is False
    assert coordinator.graph.flags(verified_only=True) == []


async def test_recovery_uses_the_injected_evidence_reader() -> None:
    """Production supplies the witness from ``run_events.result_excerpt``.

    The graph does not carry tool text (the ``upstream`` backend records lanes,
    intents and facts only), so the Runtime injects a reader.  Without it the
    recovery silently finds nothing on every real Run.
    """

    coordinator = _coordinator()
    seen: list[tuple] = []

    async def reader(refs):
        seen.append(tuple(refs))
        return "<?php\ndie('no no no');\n//n1book{afr_1_solved}"

    coordinator._evidence_reader = reader
    coordinator.graph.add_fact(
        actor="worker-1",
        content="FOUND_FLAG=n1book{afr_1_solved}",
        evidence_refs=("b5e147dd2ff5",),
    )
    assert await coordinator._recover_flag_from_evidence("RACE_WORKER_TIMEOUT") is False
    assert seen  # the reader was consulted
    assert coordinator.graph.flags(verified_only=True) == []
    assert "CTF2{afr_1_solved}" in [
        str(f.flag_value) for f in coordinator.graph.suspected_flags()
    ]


async def test_recovery_is_skipped_when_already_solved() -> None:
    coordinator = _coordinator()
    coordinator.graph.write_flag(
        actor="worker-1",
        flag="CTF2{already-there}",
        real_output="CTF2{already-there}",
    )
    assert await coordinator._recover_flag_from_evidence("RACE_WORKER_TIMEOUT") is False


async def test_recovery_reads_every_fact_not_just_the_last() -> None:
    coordinator = _coordinator()
    _retain_output(coordinator.graph, "row: n1book{union_select_is_so_cool}", call_id="call_2")
    coordinator.graph.add_fact(actor="worker-1", content="no flag here", evidence_refs=("x",))
    coordinator.graph.add_fact(
        actor="worker-2",
        content="and later: FOUND_FLAG=n1book{union_select_is_so_cool}",
        evidence_refs=("call_2",),
    )
    assert await coordinator._recover_flag_from_evidence("RACE_WORKER_TIMEOUT") is False
    assert coordinator.graph.flags(verified_only=True) == []
    assert "CTF2{union_select_is_so_cool}" in [
        str(f.flag_value) for f in coordinator.graph.suspected_flags()
    ]
