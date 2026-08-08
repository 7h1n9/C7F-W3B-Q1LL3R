from __future__ import annotations

import asyncio
import json

from app.solver.muteki.cli_driver import CLIDriver
from app.solver.muteki.container_exec import ContainerExecutor
from app.solver.muteki.control import ControlClient, ControlReceiver, InMemoryControlBus
from app.solver.muteki.graph import MutekiGraph
from app.solver.muteki.identity import EngineType, IdentityModel


def test_cli_driver_skill_mode_updates_shared_graph(tmp_path) -> None:
    graph_path = tmp_path / "graph.db"
    graph = MutekiGraph(graph_path, challenge_id="run")
    graph.close()

    async def scenario() -> None:
        result = await CLIDriver().run_skill(
            blackboard=str(graph_path),
            command="write-fact",
            skill_args=["port 80 is open", "--evidence-ref", "ev-1"],
        )
        assert result.returncode == 0

    asyncio.run(scenario())
    graph = MutekiGraph(graph_path, challenge_id="run")
    assert any("port 80" in fact.content for fact in graph.facts())
    graph.close()


def test_control_channel_targets_and_broadcasts() -> None:
    bus = InMemoryControlBus()
    first = ControlClient("worker-a", transport=bus)
    second = ControlClient("worker-b", transport=bus)
    receiver = ControlReceiver(transport=bus)

    async def scenario() -> None:
        await receiver.send_command("worker-a", "pause", {"reason": "operator"})
        message = await first.check_control()
        assert message is not None and message.type == "pause"
        assert await second.check_control() is None
        await receiver.broadcast("cancel")
        assert (await first.check_control()).type == "cancel"
        assert (await second.check_control()).type == "cancel"

    asyncio.run(scenario())


def test_identity_model_reports_supported_shape(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "C:/bin/" + name)
    assert IdentityModel.detect_engine("cursor") is EngineType.CURSOR
    capabilities = IdentityModel.get_capabilities("codex")
    assert capabilities["supports_mcp"] is True
    assert capabilities["engine"] == "codex"


def test_container_executor_builds_bounded_mount_command(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    graph_path = tmp_path / "graph.db"
    graph_path.write_bytes(b"")
    command = ContainerExecutor(image="test-worker", docker_binary="docker").build_worker_command(
        intent_id="intent-1",
        workspace_path=str(workspace),
        blackboard_path=str(graph_path),
        engine="codex",
    )
    assert command[0:4] == ["docker", "run", "--rm", "--name"]
    assert "/workspace" in " ".join(command)
    assert "MUTEKI_BLACKBOARD_DB=/muteki/shared_graph.db" in command
    assert "--privileged" not in command


def test_skill_output_is_json_serializable(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "graph.db", challenge_id="run")
    graph.add_fact(actor="worker", content=json.dumps({"type": "bounded"}))
    snapshot = graph.snapshot()
    json.dumps(snapshot)
    graph.close()
