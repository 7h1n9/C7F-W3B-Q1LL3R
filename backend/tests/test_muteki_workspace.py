from __future__ import annotations

from app.solver.muteki import EngineProfile, MutekiWorkspace


def test_prepare_creates_isolated_graph_and_stages_attachment(tmp_path) -> None:
    attachment = tmp_path / "challenge.txt"
    attachment.write_text("authorized challenge attachment", encoding="utf-8")
    session = MutekiWorkspace.prepare(
        tmp_path / "workspace",
        "challenge-1",
        attachments=[attachment],
        engines=[EngineProfile("codex", healthy=True), EngineProfile("missing", healthy=False)],
    )
    assert session.graph.db_path == tmp_path / "workspace" / "challenge-1" / "graph" / "shared_graph.db"
    assert (session.root / "attachments" / "challenge.txt").read_text(encoding="utf-8") == "authorized challenge attachment"
    assert [item.engine_id for item in session.available_engines()] == ["codex"]
    session.close()
