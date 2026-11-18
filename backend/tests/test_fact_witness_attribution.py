"""A board fact must name the command that produced it.

Before this, the paired command was already sitting in ``_pending_commands``
when ``_on_step`` saw a ``tool_result``, but it was popped *after* the marker
capture call.  Every fact therefore reached the graph unattributed: the board
could show "Flask app; GET /console returns the Werkzeug debugger" without
being able to say what was run to observe it, and the trajectory layer had to
guess by matching text back onto earlier steps.

These tests pin the three properties the fix introduced:

* the producing command survives into ``add_evidence(witness=...)``;
* a Worker's own ``echo`` is marked ``inferred`` while a probe's output is
  ``observed``, so the board can tell a self-report from a witness;
* dedupe is normalised, so the same finding spelled with different spacing or
  casing is published once.

They also pin the compatibility escape hatch: a graph whose ``add_evidence``
predates the attribution fields must still receive the fact, unattributed,
rather than silently dropping it.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.solver.muteki.worker.official_worker import (
    OfficialWorkerAdapter,
    OfficialWorkerConfig,
)


def _job() -> SimpleNamespace:
    return SimpleNamespace(worker_id="worker-1", intent_id="intent-1")


def _tool_step(*, call_id: str, command: str) -> SimpleNamespace:
    return SimpleNamespace(
        kind="tool",
        command=command,
        text="",
        tool="shell",
        session="",
        call_id=call_id,
    )


def _result_step(*, call_id: str, raw: str) -> SimpleNamespace:
    return SimpleNamespace(
        kind="tool_result",
        raw=raw,
        text="",
        tool="shell",
        session="",
        call_id=call_id,
    )


class _AttributingGraph:
    """The production shape: ``SharedGraph.add_evidence`` accepts ``witness``."""

    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def add_evidence(
        self,
        *,
        actor,
        source,
        fact,
        verified=False,
        confidence=1.0,
        witness=None,
        verifier="",
        intent_id=None,
    ):
        self.records.append(
            {
                "actor": actor,
                "fact": fact,
                "verified": verified,
                "confidence": confidence,
                "witness": witness,
                "verifier": verifier,
                "intent_id": intent_id,
            }
        )
        return len(self.records)


class _LegacyGraph:
    """A facade written before ``witness``/``verifier`` existed."""

    def __init__(self) -> None:
        self.facts: list[str] = []

    def add_evidence(self, *, actor, source, fact, verified=False, confidence=1.0, intent_id=None):
        self.facts.append(fact)
        return len(self.facts)


def test_a_fact_carries_the_command_that_produced_it() -> None:
    graph = _AttributingGraph()
    adapter = OfficialWorkerAdapter(OfficialWorkerConfig(backend="local"), shared_graph=graph)
    job = _job()

    adapter._on_step(job, _tool_step(call_id="c1", command="curl -s http://t/console | head -20"))
    adapter._on_step(
        job,
        _result_step(
            call_id="c1",
            raw="VERIFIED_FACT=Werkzeug console is reachable without auth\n",
        ),
    )
    assert adapter._flush_tool_markers(job) == 1

    [record] = graph.records
    assert record["fact"] == "Werkzeug console is reachable without auth"
    assert record["witness"] == "curl -s http://t/console | head -20"
    # A printed marker is still only a claim, whatever produced it.
    assert record["verified"] is False


def test_a_probe_result_is_observed_and_an_echo_is_inferred() -> None:
    graph = _AttributingGraph()
    adapter = OfficialWorkerAdapter(OfficialWorkerConfig(backend="local"), shared_graph=graph)
    job = _job()

    adapter._on_step(job, _tool_step(call_id="c1", command="echo 'VERIFIED_FACT=admin password is admin'"))
    adapter._on_step(job, _result_step(call_id="c1", raw="VERIFIED_FACT=admin password is admin\n"))
    adapter._on_step(job, _tool_step(call_id="c2", command="curl -s http://t/.git/config"))
    adapter._on_step(job, _result_step(call_id="c2", raw="VERIFIED_FACT=.git/config is served with a remote url\n"))
    assert adapter._flush_tool_markers(job) == 2

    by_fact = {str(record["fact"]): record for record in graph.records}
    echoed = by_fact["admin password is admin"]
    probed = by_fact[".git/config is served with a remote url"]
    assert echoed["verifier"] == "inferred"
    assert probed["verifier"] == "observed"
    # The distinction has to survive into confidence, otherwise downstream
    # ranking cannot use it.
    assert float(echoed["confidence"]) < float(probed["confidence"])


def test_markers_differing_only_in_spacing_and_case_are_one_fact() -> None:
    graph = _AttributingGraph()
    adapter = OfficialWorkerAdapter(OfficialWorkerConfig(backend="local"), shared_graph=graph)
    job = _job()

    adapter._on_step(
        job,
        _tool_step(call_id="c1", command="curl -s http://t/"),
    )
    adapter._on_step(
        job,
        _result_step(
            call_id="c1",
            raw="VERIFIED_FACT=Flask  App on port 5000\nVERIFIED_FACT=flask app on port 5000\n",
        ),
    )
    adapter._on_step(
        job,
        _tool_step(call_id="c2", command="curl -s http://t/robots.txt"),
    )
    adapter._on_step(
        job,
        _result_step(call_id="c2", raw="VERIFIED_FACT=FLASK APP ON PORT 5000\n"),
    )
    adapter._flush_tool_markers(job)

    assert [record["fact"] for record in graph.records] == ["Flask  App on port 5000"], (
        "the same finding printed three ways must reach the board once, and the "
        "first spelling must be the one that survives replay"
    )


def test_a_graph_without_witness_still_receives_the_fact() -> None:
    graph = _LegacyGraph()
    adapter = OfficialWorkerAdapter(OfficialWorkerConfig(backend="local"), shared_graph=graph)
    job = _job()

    adapter._on_step(job, _tool_step(call_id="c1", command="curl -s http://t/console"))
    adapter._on_step(
        job,
        _result_step(call_id="c1", raw="VERIFIED_FACT=console exposes the debugger\n"),
    )
    assert adapter._flush_tool_markers(job) == 1

    assert graph.facts == ["console exposes the debugger"], (
        "an unattributed fact still beats a silently discarded one: the "
        "compatibility facade must keep working after the attribution upgrade"
    )
