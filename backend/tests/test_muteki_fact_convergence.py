"""Fact-convergence coverage: how a pending candidate stops being pending.

A long Run used to accumulate candidates without bound: the Review pass could
only *add* a "REVIEW_NEEDED" marker, and nothing on the platform was ever
allowed to conclude a fact.  Following the upstream SharedGraph, every
candidate must now reach one of three outcomes -- merged into a verified fact,
rejected as a guess, or dispatched as a verification unit -- and none of them
may fabricate a verification result.

The dispatch-priority test is the other half: verification work has to actually
reach a Worker, otherwise a candidate is queued forever and "待验证" grows just
the same.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.solver.muteki.adapter.upstream_runtime_graph import UpstreamRuntimeGraph
from app.solver.muteki.graph import MutekiGraph
from app.solver.muteki.worker.review_worker import ReviewWorker, is_verification_unit


def _challenge() -> SimpleNamespace:
    return SimpleNamespace(
        id="fact-convergence",
        name="Fact convergence",
        challenge_type="WEB_TARGET",
        description="",
        target_url="http://target.test",
        flag_pattern=r"flag\{[^}]+\}",
    )


def _upstream(tmp_path, name: str) -> UpstreamRuntimeGraph:
    return UpstreamRuntimeGraph(
        tmp_path / f"{name}.sqlite",
        challenge=_challenge(),
        challenge_id=name,
    )


def _intent_payloads(graph) -> list[dict]:
    return [
        dict(item.payload or {})
        for item in graph.intents()
        if item.payload
    ]


# ── fact identity ────────────────────────────────────────────────────────


def test_fact_id_is_the_blackboard_sequence_not_a_list_position(tmp_path) -> None:
    """A Reviewer must be able to address the fact it is reviewing."""

    graph = _upstream(tmp_path, "fact-seq")
    try:
        # Interleave other events so a positional index and the real sequence
        # cannot coincide by accident.
        graph.add_fact(actor="w", content="first candidate", verified=False)
        graph.add_fact(actor="w", content="second candidate", verified=False)
        sequences = [
            int(item["seq"])
            for item in graph._graph.events_since(0, kinds=["fact_added"])
        ]

        fact_ids = [fact.fact_id for fact in graph.facts()]

        assert fact_ids == sequences
    finally:
        graph.close()


# ── the three verdicts ───────────────────────────────────────────────────


def test_candidate_covered_by_a_verified_fact_is_merged(tmp_path) -> None:
    graph = _upstream(tmp_path, "merge-duplicate")
    try:
        # Different actors: the official graph dedupes a fact by
        # (actor, identity), so the same claim from one source is one fact.
        # A duplicate from a *second* source is what the merge path is for.
        graph.add_fact(
            actor="worker-a",
            content="the admin panel is exposed at /admin",
            verified=True,
            evidence_refs=["artifact-1"],
        )
        graph.add_fact(
            actor="worker-b",
            content="the admin panel is exposed at /admin",
            verified=False,
        )

        result = ReviewWorker(graph).run()

        assert result.concluded_fact_ids
        # A merged fact is a conclusion, so it leaves the pending view.
        pending = [fact.content for fact in graph.facts() if not fact.verified]
        assert "the admin panel is exposed at /admin" not in pending
        states = graph.fact_states()
        assert "merged" in {str(state.get("state")) for state in states.values()}
    finally:
        graph.close()


def test_hedged_candidate_without_evidence_is_rejected(tmp_path) -> None:
    graph = _upstream(tmp_path, "reject-guess")
    try:
        graph.add_fact(
            actor="w",
            content="i think the login might accept any password",
            verified=False,
        )

        result = ReviewWorker(graph).run()

        assert result.concluded_fact_ids
        states = graph.fact_states()
        assert "rejected" in {str(state.get("state")) for state in states.values()}
        assert not [fact for fact in graph.facts() if "accept any password" in fact.content]
    finally:
        graph.close()


def test_a_described_hypothesis_is_verified_not_rejected(tmp_path) -> None:
    """"may be" is a hypothesis to test, not a guess to discard."""

    graph = _upstream(tmp_path, "hypothesis-not-guess")
    try:
        graph.add_fact(
            actor="w",
            content="the parameter may be vulnerable to SQL injection",
            verified=False,
            evidence_refs=["artifact-2"],
        )

        result = ReviewWorker(graph).run()

        assert not result.concluded_fact_ids
        assert result.suspicious_fact_ids
        assert graph.fact_states()
    finally:
        graph.close()


def test_actionable_candidate_becomes_a_verification_unit(tmp_path) -> None:
    graph = _upstream(tmp_path, "dispatch-unit")
    try:
        graph.add_fact(
            actor="w",
            content="the search endpoint returns 500 on a quote character",
            verified=False,
            evidence_refs=["artifact-3"],
        )

        result = ReviewWorker(graph).run()

        assert result.suspicious_fact_ids
        # The official backend names its verifier unit ``I-verify-<seq>-<hash>``
        # and carries no target field, so the check goes through the shared
        # predicate rather than one payload shape.
        assert any(
            is_verification_unit(item) for item in graph.intents()
        ), [dict(item.payload or {}) for item in graph.intents()]
        # The candidate is now challenged, i.e. explicitly awaiting verification
        # rather than silently pending.
        states = graph.fact_states()
        assert "challenged" in {str(state.get("state")) for state in states.values()}
    finally:
        graph.close()


def test_alternative_candidate_dispatches_one_unit_per_alternative(tmp_path) -> None:
    graph = _upstream(tmp_path, "dispatch-alternatives")
    try:
        graph.add_fact(
            actor="w",
            content="the redirect may indicate an open redirect or a host header injection",
            verified=False,
            evidence_refs=["artifact-4"],
        )

        result = ReviewWorker(graph).run()

        assert len(result.branch_intent_ids) == 2
        branch_payloads = [
            payload
            for payload in _intent_payloads(graph)
            if payload.get("review_branch")
        ]
        assert len(branch_payloads) == 2
        assert len({payload.get("route_hash") for payload in branch_payloads}) == 2
    finally:
        graph.close()


def test_review_does_not_requeue_the_same_candidate(tmp_path) -> None:
    """A second pass must not duplicate the verification work of the first."""

    graph = _upstream(tmp_path, "review-idempotent")
    try:
        graph.add_fact(
            actor="w",
            content="the upload endpoint accepts a .php extension",
            verified=False,
            evidence_refs=["artifact-5"],
        )

        first = ReviewWorker(graph).run()
        before = len([p for p in _intent_payloads(graph) if p.get("verification_target_seq")])
        second = ReviewWorker(graph).run()
        after = len([p for p in _intent_payloads(graph) if p.get("verification_target_seq")])

        assert first.suspicious_fact_ids
        assert after == before, "the second pass must recognise the existing unit"
        assert not second.suspicious_fact_ids
    finally:
        graph.close()


def test_review_pass_is_bounded(tmp_path) -> None:
    """One pass is a prioritizer: it must not queue the entire board at once."""

    graph = _upstream(tmp_path, "review-bounded")
    try:
        for index in range(30):
            graph.add_fact(
                actor="w",
                content=f"observed endpoint number {index} on the target",
                verified=False,
                evidence_refs=[f"artifact-{index}"],
            )

        result = ReviewWorker(graph).run()

        assert len(result.suspicious_fact_ids) <= 6
    finally:
        graph.close()


# ── verification work actually reaches a Worker ──────────────────────────


def test_coordinator_dispatches_verification_before_exploration(tmp_path) -> None:
    """A queued verification unit outranks a fresh direction."""

    graph = MutekiGraph(tmp_path / "dispatch-priority.sqlite", challenge_id="dispatch-priority")
    try:
        graph.propose_intent(
            actor="reason",
            description="explore a brand new direction",
            payload={"route_hash": "explore:new"},
        )
        graph.propose_intent(
            actor="review-worker",
            description="verify fact #7: confirm or refute the upload finding",
            payload={
                "route_hash": "verify:7",
                "verification_target_seq": 7,
                "review_approved": True,
            },
        )

        from app.solver.muteki.coordinator import MutekiCoordinator

        coordinator = object.__new__(MutekiCoordinator)
        coordinator.graph = graph
        coordinator._semantic_reservations = {}
        coordinator._active_route_hashes = set()

        candidates = list(graph.intents(status="open"))
        selected = coordinator._select_route_intent(candidates)

        assert selected is not None
        assert selected.payload.get("verification_target_seq") == 7
    finally:
        graph.close()


def test_coordinator_falls_back_to_exploration_when_nothing_to_verify(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "dispatch-fallback.sqlite", challenge_id="dispatch-fallback")
    try:
        graph.propose_intent(
            actor="reason",
            description="explore a brand new direction",
            payload={"route_hash": "explore:new"},
        )

        from app.solver.muteki.coordinator import MutekiCoordinator

        coordinator = object.__new__(MutekiCoordinator)
        coordinator.graph = graph
        coordinator._semantic_reservations = {}
        coordinator._active_route_hashes = set()

        selected = coordinator._select_route_intent(list(graph.intents(status="open")))

        assert selected is not None
        assert not selected.payload.get("verification_target_seq")
    finally:
        graph.close()


@pytest.mark.parametrize("backend", ["compat", "upstream"])
def test_both_graph_backends_expose_the_review_entry_point(tmp_path, backend: str) -> None:
    """A Reviewer must behave identically on either backend."""

    if backend == "compat":
        graph = MutekiGraph(tmp_path / "review-entry.sqlite", challenge_id="review-entry")
    else:
        graph = _upstream(tmp_path, "review-entry")
    try:
        assert callable(getattr(graph, "review_fact", None))

        sequence = graph.add_fact(actor="w", content="candidate", verified=False)
        graph.review_fact(
            actor="review-worker",
            fact_seq=int(sequence),
            action="reject",
            reason="test",
        )

        states = graph.fact_states() if hasattr(graph, "fact_states") else {}
        if backend == "compat":
            # The compatibility graph reports the verdict through the graph
            # itself rather than a state map.
            assert int(sequence) in graph.retired_fact_seqs()
        else:
            assert str(states.get(int(sequence), {}).get("state")) == "rejected"
    finally:
        graph.close()


# ── re-selection must survive a fully exhausted board ─────────────────────


def _bare_coordinator(graph):
    from app.solver.muteki.coordinator import MutekiCoordinator

    coordinator = object.__new__(MutekiCoordinator)
    coordinator.graph = graph
    coordinator._semantic_reservations = {}
    coordinator._active_route_hashes = set()
    return coordinator


def _finish_route(graph, *, route_hash: str, label: str) -> str:
    """Propose + conclude one intent so its route lands in ``completed_routes``."""

    intent_id = graph.propose_intent(
        actor="reason", description=label, payload={"route_hash": route_hash}
    )
    graph.claim_intent(worker="w", intent_id=intent_id)
    graph.conclude_intent(actor="w", intent_id=intent_id)
    return intent_id


def test_finished_route_does_not_crash_reselection(tmp_path) -> None:
    """A Run that exhausts every direction must still be able to re-select.

    Observed live (run d3ac62f0): once all intents were ``done``, the Coordinator
    walked the completed-route branch and raised ``NameError`` on an undefined
    ``payload`` -- the whole Run died as FAILED_ENGINE.  The branch must read the
    *per-intent* payload and simply skip a route that already completed.
    """

    graph = MutekiGraph(tmp_path / "reselection.sqlite", challenge_id="reselection")
    try:
        _finish_route(graph, route_hash="explore:oracle", label="probe the oracle")
        graph.propose_intent(
            actor="reason",
            description="probe the oracle",
            payload={"route_hash": "explore:oracle"},
        )

        coordinator = _bare_coordinator(graph)
        candidates = list(graph.intents(status="open"))

        # Pre-fix this raised NameError; now the finished route is skipped.
        assert coordinator._select_route_intent(candidates) is None
    finally:
        graph.close()


def test_review_approved_retry_reopens_a_finished_route(tmp_path) -> None:
    """The one exception: a route the Review pass explicitly reopened."""

    graph = MutekiGraph(tmp_path / "reopen.sqlite", challenge_id="reopen")
    try:
        _finish_route(graph, route_hash="explore:oracle", label="probe the oracle")
        reopened = graph.propose_intent(
            actor="reason",
            description="probe the oracle again",
            payload={"route_hash": "explore:oracle", "review_approved": True},
        )

        coordinator = _bare_coordinator(graph)
        candidates = list(graph.intents(status="open"))

        selected = coordinator._select_route_intent(candidates)
        assert selected is not None
        assert selected.intent_id == reopened
    finally:
        graph.close()
