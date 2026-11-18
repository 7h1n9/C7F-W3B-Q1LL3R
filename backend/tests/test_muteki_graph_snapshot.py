from types import SimpleNamespace

from app.solver.muteki.adapter.graph_snapshot import _summary_zh, read_native_graph_snapshot
from app.solver.muteki.adapter.upstream_runtime_graph import UpstreamRuntimeGraph


def _challenge() -> SimpleNamespace:
    return SimpleNamespace(
        id="snapshot-challenge",
        name="Snapshot target",
        challenge_type="WEB_TARGET",
        description="",
        target_url="http://target.test",
        flag_pattern=r"flag\{[^}]+\}",
    )


def test_native_graph_snapshot_is_read_only_and_projects_blackboard(tmp_path) -> None:
    path = tmp_path / "upstream.sqlite"
    challenge = _challenge()
    graph = UpstreamRuntimeGraph(path, challenge=challenge, challenge_id="run-snapshot")
    try:
        graph.add_fact(
            actor="race",
            content="/tickets returned 200",
            verified=True,
            evidence_refs=["evidence-1"],
        )
        intent_id = graph.propose_intent(actor="reason", description="inspect tickets")
        graph.claim_intent(worker="worker-1", intent_id=intent_id)
    finally:
        graph.close()

    before = path.read_bytes()
    snapshot = read_native_graph_snapshot(
        db_path=path,
        challenge=challenge,
        run_id="run-snapshot",
    )

    assert snapshot["available"] is True
    assert snapshot["facts"] == [
        {
            "sequence": snapshot["facts"][0]["sequence"],
            "content": "/tickets returned 200",
            "verified": True,
            "evidence_refs": ["evidence-1"],
            # Review lifecycle state travels with the fact so the UI can tell a
            # pending candidate from a challenged one.  Terminal states
            # (rejected/merged/superseded) are dropped from the list entirely.
            "state": "unresolved",
        }
    ]
    assert snapshot["key_conditions"] == [
        {
            "sequence": snapshot["facts"][0]["sequence"],
            "content": "/tickets returned 200",
            "summary_zh": "/tickets 返回 200",
            "verified": True,
            "evidence_refs": ["evidence-1"],
            "confidence": 1.0,
            "source_worker_id": "race",
        }
    ]
    assert snapshot["intents"][0]["id"] == intent_id
    assert snapshot["intents"][0]["status"] == "claimed"
    assert path.read_bytes() == before


def test_terminal_fact_leaves_the_snapshot(tmp_path) -> None:
    """A concluded candidate must not keep reading as a pending question."""

    path = tmp_path / "terminal.sqlite"
    challenge = _challenge()
    graph = UpstreamRuntimeGraph(path, challenge=challenge, challenge_id="run-terminal")
    try:
        sequence = graph.add_fact(
            actor="race",
            content="maybe the /admin route is reachable",
            verified=False,
        )
        graph.review_fact(
            actor="review-worker",
            fact_seq=int(sequence),
            action="reject",
            reason="speculation without evidence",
        )
    finally:
        graph.close()

    snapshot = read_native_graph_snapshot(
        db_path=path,
        challenge=challenge,
        run_id="run-terminal",
    )

    assert snapshot["available"] is True
    assert [fact["content"] for fact in snapshot["facts"]] == []
    assert snapshot["key_conditions"] == []


def test_challenged_fact_keeps_its_review_state_in_the_snapshot(tmp_path) -> None:
    path = tmp_path / "challenged.sqlite"
    challenge = _challenge()
    graph = UpstreamRuntimeGraph(path, challenge=challenge, challenge_id="run-challenged")
    try:
        sequence = graph.add_fact(
            actor="race",
            content="/tickets returned 200 for an unauthenticated request",
            verified=False,
            evidence_refs=["evidence-9"],
        )
        graph.review_fact(
            actor="review-worker",
            fact_seq=int(sequence),
            action="challenge",
            reason="candidate awaiting verification",
            verification_goal="confirm the unauthenticated 200",
        )
    finally:
        graph.close()

    snapshot = read_native_graph_snapshot(
        db_path=path,
        challenge=challenge,
        run_id="run-challenged",
    )

    assert snapshot["facts"][0]["state"] == "challenged"
    assert snapshot["facts"][0]["verified"] is False


def test_native_graph_snapshot_degrades_when_graph_has_not_started(tmp_path) -> None:
    snapshot = read_native_graph_snapshot(
        db_path=tmp_path / "missing.sqlite",
        challenge=_challenge(),
        run_id="run-missing",
    )

    assert snapshot["available"] is False
    assert snapshot["reason"] == "GRAPH_NOT_INITIALIZED"


def test_native_graph_snapshot_key_conditions_require_active_verified_evidence(tmp_path) -> None:
    path = tmp_path / "filtered.sqlite"
    challenge = _challenge()
    graph = UpstreamRuntimeGraph(path, challenge=challenge, challenge_id="run-filtered")
    try:
        verified = graph.add_fact(actor="worker", content="admin account endpoint", verified=True, evidence_refs=["ev-1"])
        graph.add_fact(actor="worker", content="hypothesis: password may exist", verified=True, evidence_refs=["ev-2"])
        graph.add_fact(actor="worker", content="/schema returned 200", verified=True)
        graph.native_graph().reject_fact(actor="review", fact_seq=verified, reason="review rejected")
    finally:
        graph.close()

    snapshot = read_native_graph_snapshot(db_path=path, challenge=challenge, run_id="run-filtered")

    assert snapshot["key_conditions"] == []


def test_board_summary_formats_structured_http_fact() -> None:
    assert _summary_zh('{"endpoint":"/api/files","method":"POST","status_code":401}') == (
        "接口：POST /api/files；HTTP 401，需要认证。"
    )


def test_board_summary_formats_evidence_sentence() -> None:
    summary = _summary_zh(
        "[codex] Dynamic routes `/download/<id>` and `/preview/<id>` exist; "
        "unauthenticated GET returns `401`"
    )
    assert summary == "动态路由：/download/<id>, /preview/<id>；未登录 GET 返回 HTTP 401。"
