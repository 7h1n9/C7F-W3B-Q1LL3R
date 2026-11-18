"""Distillation must not reopen a Coordinator-finalized Muteki graph.

These tests exercise the *real* distillation path: the previous version of
``_distill`` instantiated ``muteki.swarm.shared_graph.SharedGraph``, which is a
typing Protocol, so every call raised TypeError into a bare ``except`` and no
template was ever written.  The tests therefore assert on observable effects
(the store being written, the graph being untouched) rather than on the call.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from app.solver.muteki.events import EventType
from app.solver.muteki.graph import MutekiGraph
from app.solver.muteki.runtime.muteki_runtime import MutekiRuntime
from muteki.models.solve_graph import Challenge as UpstreamChallenge
from muteki.swarm.shared_graph import SQLiteSharedGraph

_PROJECT_CHALLENGE = SimpleNamespace(
    id="challenge-distill",
    name="web-login",
    challenge_type="WEB_TARGET",
    description="a login page with a session cookie",
    attachments=[],
    target_url="http://target.test/",
    flag_pattern=r"flag\{.*?\}",
)


def _seed_upstream_graph(db_path) -> SQLiteSharedGraph:
    """A real upstream graph with one verified fact and one ruled-out route."""

    graph = SQLiteSharedGraph.open(
        db_path=db_path,
        challenge=UpstreamChallenge(
            id="challenge-distill",
            name="web-login",
            category="web",
            description="a login page with a session cookie",
        ),
    )
    graph.add_evidence(
        actor="worker-1", source="http", fact="login form found", verified=True
    )
    graph.add_dead_end(actor="worker-1", reason="LDAP bind injection returned 500")
    return graph


def _distill_runtime(graph: MutekiGraph) -> MutekiRuntime:
    runtime = object.__new__(MutekiRuntime)
    runtime._run_id = "run-distill"
    runtime._graph = graph
    runtime.challenge = _PROJECT_CHALLENGE
    runtime._workspace_path = "/tmp/workspace"
    return runtime


def _finalized_graph(tmp_path, name: str) -> MutekiGraph:
    graph = MutekiGraph(tmp_path / name, challenge_id="challenge-distill")
    graph.emit_event(
        actor="coordinator",
        event_type=EventType.RUN_FINISHED,
        payload={"reason": "SOLVED", "flag_found": True},
    )
    return graph


def test_distill_success_does_not_emit_second_run_finished(tmp_path) -> None:
    graph = _finalized_graph(tmp_path, "distill-success.sqlite")
    terminal_revision = graph.revision()
    runtime = _distill_runtime(graph)
    upstream_path = tmp_path / "upstream" / "shared_graph.db"
    _seed_upstream_graph(upstream_path)

    with patch("muteki.learning.distill.TemplateStore") as store_cls:
        runtime._distill(str(upstream_path), reason="SOLVED", solved=True)

    assert graph.revision() == terminal_revision
    assert [
        event.event_type
        for event in graph.events_since()
        if event.event_type == EventType.RUN_FINISHED
    ] == [EventType.RUN_FINISHED]
    # The real loop ran: a solved Run must actually reach the store.
    assert store_cls.return_value.save.call_count == 1
    saved = store_cls.return_value.save.call_args.args[0]
    assert saved.kind == "case"


def test_distill_failure_does_not_write_graph_dead_end(tmp_path) -> None:
    graph = _finalized_graph(tmp_path, "distill-failure.sqlite")
    terminal_revision = graph.revision()
    runtime = _distill_runtime(graph)
    upstream_path = tmp_path / "upstream" / "shared_graph.db"
    _seed_upstream_graph(upstream_path)

    with patch(
        "muteki.learning.distill.TemplateStore",
        side_effect=RuntimeError("store unavailable"),
    ):
        runtime._distill(str(upstream_path), reason="SOLVED", solved=True)

    assert graph.revision() == terminal_revision
    assert graph.dead_ends() == []


def test_unsolved_run_stores_a_negative_template(tmp_path) -> None:
    """Rejection sampling: a failed Run contributes negatives, not cases."""

    graph = _finalized_graph(tmp_path, "distill-unsolved.sqlite")
    runtime = _distill_runtime(graph)
    upstream_path = tmp_path / "upstream" / "shared_graph.db"
    _seed_upstream_graph(upstream_path)

    with patch("muteki.learning.distill.TemplateStore") as store_cls:
        runtime._distill(str(upstream_path), reason="TIMEOUT", solved=False)

    assert store_cls.return_value.save.call_count == 1
    saved = store_cls.return_value.save.call_args.args[0]
    assert saved.kind == "negative"
    assert any("LDAP" in step for step in saved.steps)


def test_distill_skips_when_the_graph_file_is_absent(tmp_path) -> None:
    """A missing graph must never be materialized into a junk template."""

    graph = _finalized_graph(tmp_path, "distill-absent.sqlite")
    runtime = _distill_runtime(graph)

    with patch("muteki.learning.distill.TemplateStore") as store_cls:
        runtime._distill(str(tmp_path / "nope" / "shared_graph.db"), reason="TIMEOUT")

    assert store_cls.return_value.save.call_count == 0
