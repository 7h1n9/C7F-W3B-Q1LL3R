from types import SimpleNamespace

from app.solver.muteki.adapter.upstream_events import (
    UpstreamIntentAdapter,
    project_upstream_events,
)
from app.solver.muteki.adapter.upstream_graph import project_graph
from app.solver.muteki.events import EventType
from app.solver.muteki.graph import MutekiGraph
from app.solver.muteki.upstream_bridge import (
    UPSTREAM_MUTEKI_VERSION,
    open_upstream_shared_graph,
    to_upstream_challenge,
    upstream_available,
)


def test_vendored_upstream_package_is_importable() -> None:
    assert UPSTREAM_MUTEKI_VERSION == "0.2.5"
    assert upstream_available()


def test_existing_challenge_maps_to_upstream_solve_graph_model() -> None:
    challenge = SimpleNamespace(
        id="challenge-1",
        name="Preview center",
        challenge_type="WEB_TARGET",
        description="A bounded web target",
        target_url="http://target.test",
        flag_pattern=r"flag\{[^}]+\}",
        attachments=[SimpleNamespace(original_name="brief.pdf")],
    )

    mapped = to_upstream_challenge(challenge)

    assert mapped.id == "challenge-1"
    assert mapped.category == "web"
    assert mapped.target == "http://target.test"
    assert mapped.attachments == ["brief.pdf"]
    assert mapped.flag_format == r"flag\{[^}]+\}"


def test_bridge_does_not_copy_metadata_into_upstream_challenge() -> None:
    challenge = SimpleNamespace(
        id="challenge-2",
        name="Metadata target",
        challenge_type="WEB_TARGET",
        description="",
        target_url="http://target.test",
        flag_pattern=r"flag\{[^}]+\}",
        metadata_json={"dbms": "mysql", "answer": "must-not-cross-boundary"},
    )

    mapped = to_upstream_challenge(challenge)

    assert "metadata_json" not in mapped.model_fields_set
    assert "answer" not in mapped.model_dump_json()


def test_current_graph_projects_to_upstream_solve_graph_without_becoming_authority(tmp_path) -> None:
    challenge = SimpleNamespace(
        id="challenge-3",
        name="Projection target",
        challenge_type="WEB_TARGET",
        description="",
        target_url="http://target.test",
        flag_pattern=r"flag\{[^}]+\}",
    )
    graph = MutekiGraph(tmp_path / "current.sqlite", challenge_id=challenge.id)
    try:
        graph.add_fact(
            actor="race",
            content="HTTP endpoint /health returned 200",
            verified=True,
            evidence_refs=["evidence-1"],
        )
        graph.add_dead_end(actor="reason", description="No login form at /admin")

        projected = project_graph(graph, challenge)

        assert projected.challenge.id == challenge.id
        assert len(projected.evidence) == 1
        assert projected.evidence[0].artifact_id == "evidence-1"
        assert projected.evidence[0].verified is True
        assert projected.dead_ends == ["No login form at /admin"]
        assert graph.facts()[0].content == "HTTP endpoint /health returned 200"
    finally:
        graph.close()


def test_upstream_shared_graph_can_be_opened_behind_bridge(tmp_path) -> None:
    challenge = SimpleNamespace(
        id="challenge-4",
        name="Official graph target",
        challenge_type="WEB_TARGET",
        description="",
        target_url="http://target.test",
        flag_pattern=r"flag\{[^}]+\}",
    )
    upstream_graph = open_upstream_shared_graph(
        db_path=str(tmp_path / "upstream.sqlite"),
        challenge=challenge,
    )
    try:
        upstream_graph.add_evidence(
            actor="race",
            source="http_request",
            fact="HTTP endpoint /health returned 200",
            verified=True,
        )
        snapshot = upstream_graph.snapshot()

        assert snapshot.challenge.id == challenge.id
        assert len(snapshot.evidence) == 1
        assert snapshot.evidence[0].verified is True
    finally:
        upstream_graph.close()


def test_upstream_intent_and_event_projection_preserve_order_and_safe_fields(tmp_path) -> None:
    challenge = SimpleNamespace(
        id="challenge-5",
        name="Event target",
        challenge_type="WEB_TARGET",
        description="",
        target_url="http://target.test",
        flag_pattern=r"flag\{[^}]+\}",
    )
    upstream_graph = open_upstream_shared_graph(
        db_path=str(tmp_path / "events.sqlite"),
        challenge=challenge,
    )
    try:
        UpstreamIntentAdapter.propose(
            upstream_graph,
            actor="reason",
            intent_id="intent-1",
            description="inspect endpoint",
            payload={"worker_class": "code", "raw_result": "must-not-leak"},
        )
        assert UpstreamIntentAdapter.claim(
            upstream_graph,
            worker="worker-1",
            intent_id="intent-1",
        )
        UpstreamIntentAdapter.conclude(
            upstream_graph,
            actor="worker-1",
            intent_id="intent-1",
            result="explored",
        )

        projected = project_upstream_events(
            upstream_graph.events(),
            challenge_id=challenge.id,
        )

        assert [event.event_type for event in projected] == [
            EventType.INTENT_PROPOSED,
            EventType.INTENT_CLAIMED,
            EventType.INTENT_CONCLUDED,
        ]
        assert projected[0].payload["payload"]["worker_class"] == "code"
        assert "raw_result" not in projected[0].payload["payload"]
        assert projected[1].sequence < projected[2].sequence
        assert projected[0].challenge_id == challenge.id
    finally:
        upstream_graph.close()
