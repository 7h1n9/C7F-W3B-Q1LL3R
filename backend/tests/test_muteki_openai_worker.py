from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from app.solver.muteki.worker import openai_compatible
from muteki.core.llm import LLMResponse, ToolCall
from muteki.solver.cli_driver import CliResult


class _Graph:
    def __init__(self) -> None:
        self.facts: list[str] = []

    def snapshot(self):
        return SimpleNamespace(evidence=[])

    def add_evidence(self, *, fact: str, **_kwargs) -> int:
        self.facts.append(fact)
        return len(self.facts)


class _MappingGraph(_Graph):
    def snapshot(self):
        return {
            "facts": [
                {
                    "content": '{"endpoint":"/approvals","status_code":200}',
                    "verified": True,
                    "evidence_refs": ["evidence-1"],
                }
            ],
            "evidence": [],
            "intents": [
                {"description": "EXPLORE_ENDPOINTS /", "status": "done"},
            ],
            "dead_ends": [{"description": "do not repeat the completed route"}],
        }


class _Client:
    def __init__(self, *args, **kwargs) -> None:
        self.calls = 0

    async def chat(self, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="",
                reasoning="",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="http_request",
                        arguments=json.dumps({"method": "GET", "url": "/"}),
                    )
                ],
                finish_reason="tool_calls",
                model="deepseek-v4-flash",
            )
        return LLMResponse(
            content="bounded observation recorded",
            reasoning="",
            tool_calls=[],
            finish_reason="stop",
            model="deepseek-v4-flash",
        )

    async def aclose(self) -> None:
        return None


def test_openai_compatible_worker_uses_chat_tool_and_persists_safe_fact(tmp_path, monkeypatch) -> None:
    graph = _Graph()
    monkeypatch.setattr(openai_compatible, "LLMClient", _Client)

    def run_command(*_args, **_kwargs):
        return CliResult(
            text=json.dumps(
                {
                    "success": True,
                    "status_code": 200,
                    "final_url": "http://target.test/",
                    "headers": {"content-type": "text/html"},
                    "cookie_names": ["session"],
                    "body": "<html><form action='/login'></form></html>",
                }
            )
        )

    monkeypatch.setattr(openai_compatible, "run_cli_streaming_container", run_command)
    from muteki.core.cost import CostController

    worker = openai_compatible.OpenAICompatibleWorker(
        graph=graph,
        container=object(),
        workspace=str(tmp_path),
        worker_id="worker-chat",
        intent_id="intent-chat",
        target_url="http://target.test",
        base_url="https://provider.test/v1",
        api_key="secret-not-persisted",
        model="deepseek-v4-flash",
        max_turns=2,
        timeout_seconds=10,
        cost=CostController(),
        run_id="run-chat",
    )

    result = asyncio.run(worker.run(goal="inspect the target"))

    assert result.success is True
    assert result.engine == "deepseek-v4-flash"
    assert result.metadata["request_count"] == 1
    assert graph.facts
    assert "http_request" in graph.facts[0]
    artifact = tmp_path / ".muteki-artifacts" / "chat-worker-chat" / "http-evidence.json"
    assert artifact.exists()
    assert "secret-not-persisted" not in artifact.read_text(encoding="utf-8")


def test_openai_compatible_worker_cancel_interrupts_model_loop(tmp_path, monkeypatch) -> None:
    class BlockingClient:
        def __init__(self, **_kwargs):
            pass

        async def chat(self, **_kwargs):
            await asyncio.sleep(30)

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(openai_compatible, "LLMClient", BlockingClient)
    from muteki.core.cost import CostController

    worker = openai_compatible.OpenAICompatibleWorker(
        graph=_Graph(),
        container=object(),
        workspace=str(tmp_path),
        worker_id="worker-cancel",
        intent_id="intent-cancel",
        target_url="http://target.test",
        base_url="https://provider.test/v1",
        api_key="secret-not-persisted",
        model="deepseek-v4-flash",
        max_turns=2,
        timeout_seconds=30,
        cost=CostController(),
        run_id="run-cancel",
    )

    async def scenario():
        task = asyncio.create_task(worker.run(goal="bounded action"))
        await asyncio.sleep(0.05)
        worker.cancel()
        result = await task
        assert result.success is False
        assert result.output == "INTERRUPTED"

    asyncio.run(scenario())


def test_openai_compatible_worker_rejects_cross_host_request(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(openai_compatible, "LLMClient", _Client)
    called = False

    def run_command(*_args, **_kwargs):
        nonlocal called
        called = True
        return CliResult(text="{}")

    monkeypatch.setattr(openai_compatible, "run_cli_streaming_container", run_command)
    from muteki.core.cost import CostController

    worker = openai_compatible.OpenAICompatibleWorker(
        graph=_Graph(),
        container=object(),
        workspace=str(tmp_path),
        worker_id="worker-host-boundary",
        intent_id="intent-host-boundary",
        target_url="http://target.test",
        base_url="https://provider.test/v1",
        api_key="secret-not-persisted",
        model="deepseek-v4-flash",
        max_turns=1,
        timeout_seconds=10,
        cost=CostController(),
        run_id="run-host-boundary",
    )

    result = asyncio.run(worker._http("http_request", {"method": "GET", "url": "http://other.test/"}))

    assert result["error"] == "TARGET_HOST_NOT_ALLOWED"
    assert called is False


def test_openai_compatible_worker_includes_mapping_blackboard_facts(tmp_path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    class CapturingClient(_Client):
        async def chat(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            return LLMResponse(
                content="no action",
                reasoning="",
                tool_calls=[],
                finish_reason="stop",
                model="deepseek-v4-flash",
            )

    monkeypatch.setattr(openai_compatible, "LLMClient", CapturingClient)
    from muteki.core.cost import CostController

    worker = openai_compatible.OpenAICompatibleWorker(
        graph=_MappingGraph(),
        container=object(),
        workspace=str(tmp_path),
        worker_id="worker-mapping",
        intent_id="intent-mapping",
        target_url="http://target.test",
        base_url="https://provider.test/v1",
        api_key="secret-not-persisted",
        model="deepseek-v4-flash",
        max_turns=1,
        timeout_seconds=10,
        cost=CostController(),
        run_id="run-mapping",
    )

    result = asyncio.run(worker.run(goal="continue from the shared graph"))

    assert result.success is False
    prompt = str(captured["messages"][-1]["content"])
    assert "/approvals" in prompt
    assert "evidence-1" in prompt
    assert "EXPLORE_ENDPOINTS /" in prompt
    assert "do not repeat the completed route" in prompt


def test_openai_compatible_worker_refreshes_shared_endpoint_facts(tmp_path, monkeypatch) -> None:
    class SharedGraph(_Graph):
        def snapshot(self):
            return {"facts": [{"content": fact} for fact in self.facts]}

        def try_claim_activity(self, *, worker, key, lease_s):
            del worker, lease_s
            if key in self.facts:
                return False
            self.facts.append(key)
            return True

        def release_activity(self, *, worker, key):
            del worker, key

    graph = SharedGraph()
    from muteki.core.cost import CostController

    monkeypatch.setattr(
        openai_compatible,
        "run_cli_streaming_container",
        lambda *_args, **_kwargs: CliResult(
            text=json.dumps({"success": True, "status_code": 200, "body": "ok"})
        ),
    )
    kwargs = {
        "graph": graph,
        "container": object(),
        "workspace": str(tmp_path),
        "target_url": "http://target.test",
        "base_url": "https://provider.test/v1",
        "api_key": "secret",
        "model": "model",
        "max_turns": 1,
        "timeout_seconds": 10,
        "run_id": "run",
    }
    first = openai_compatible.OpenAICompatibleWorker(
        **kwargs, worker_id="worker-a", intent_id="intent-a", cost=CostController()
    )
    second = openai_compatible.OpenAICompatibleWorker(
        **kwargs, worker_id="worker-b", intent_id="intent-b", cost=CostController()
    )
    assert asyncio.run(first._http("http_request", {"method": "GET", "url": "/"}))["success"]
    duplicate = asyncio.run(second._http("http_request", {"method": "GET", "url": "/"}))
    assert duplicate["error"] == "ROUTE_EXHAUSTED"


def test_openai_compatible_worker_rechecks_blackboard_after_activity_claim(tmp_path, monkeypatch) -> None:
    """A fact written between the initial read and lock claim must win."""

    class HandoffGraph(_Graph):
        def __init__(self) -> None:
            super().__init__()
            self.claimed = False
            self.executed = False

        def snapshot(self):
            facts = list(self.facts)
            if self.claimed and not self.executed:
                facts.append('{"endpoint_key":"GET:/:"}')
            return {"facts": [{"content": fact} for fact in facts]}

        def try_claim_activity(self, *, worker, key, lease_s):
            del worker, key, lease_s
            self.claimed = True
            return True

        def release_activity(self, *, worker, key):
            del worker, key

    graph = HandoffGraph()
    called = False

    def run_command(*_args, **_kwargs):
        nonlocal called
        called = True
        graph.executed = True
        return CliResult(text=json.dumps({"success": True, "status_code": 200, "body": "ok"}))

    monkeypatch.setattr(openai_compatible, "run_cli_streaming_container", run_command)
    from muteki.core.cost import CostController

    worker = openai_compatible.OpenAICompatibleWorker(
        graph=graph,
        container=object(),
        workspace=str(tmp_path),
        worker_id="worker-handoff",
        intent_id="intent-handoff",
        target_url="http://target.test",
        base_url="https://provider.test/v1",
        api_key="secret",
        model="model",
        max_turns=1,
        timeout_seconds=10,
        cost=CostController(),
        run_id="run",
    )

    result = asyncio.run(worker._http("http_request", {"method": "GET", "url": "/"}))

    assert result["error"] == "ROUTE_EXHAUSTED"
    assert called is False


def test_openai_compatible_worker_reads_native_verified_evidence(tmp_path, monkeypatch) -> None:
    class NativeGraph:
        def verified_evidence(self):
            return [{"fact": '{"endpoint_key":"POST:/api/check:"}'}]

        def try_claim_activity(self, **_kwargs):
            return True

        def release_activity(self, **_kwargs):
            return None

    called = False

    def run_command(*_args, **_kwargs):
        nonlocal called
        called = True
        return CliResult(text=json.dumps({"success": True, "status_code": 200, "body": "ok"}))

    monkeypatch.setattr(openai_compatible, "run_cli_streaming_container", run_command)
    from muteki.core.cost import CostController

    worker = openai_compatible.OpenAICompatibleWorker(
        graph=NativeGraph(),
        container=object(),
        workspace=str(tmp_path),
        worker_id="worker-native-graph",
        intent_id="intent-native-graph",
        target_url="http://target.test",
        base_url="https://provider.test/v1",
        api_key="secret",
        model="model",
        max_turns=1,
        timeout_seconds=10,
        cost=CostController(),
        run_id="run",
    )

    result = asyncio.run(worker._http("http_request", {"method": "POST", "url": "/api/check"}))

    assert result["error"] == "ROUTE_EXHAUSTED"
    assert called is False


def test_openai_compatible_worker_keeps_activity_claim_after_success(tmp_path, monkeypatch) -> None:
    """One model turn cannot execute the same route twice after a success."""

    class PersistentActivityGraph(_Graph):
        def __init__(self) -> None:
            super().__init__()
            self.active: set[str] = set()

        def try_claim_activity(self, *, worker, key, lease_s):
            del worker, lease_s
            if key in self.active:
                return False
            self.active.add(key)
            return True

        def release_activity(self, *, worker, key):
            del worker
            self.active.discard(key)

    graph = PersistentActivityGraph()
    calls = 0

    def run_command(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return CliResult(text=json.dumps({"success": True, "status_code": 200, "body": "ok"}))

    monkeypatch.setattr(openai_compatible, "run_cli_streaming_container", run_command)
    from muteki.core.cost import CostController

    worker = openai_compatible.OpenAICompatibleWorker(
        graph=graph,
        container=object(),
        workspace=str(tmp_path),
        worker_id="worker-persistent-route",
        intent_id="intent-persistent-route",
        target_url="http://target.test",
        base_url="https://provider.test/v1",
        api_key="secret",
        model="model",
        max_turns=1,
        timeout_seconds=10,
        cost=CostController(),
        run_id="run",
    )

    first = asyncio.run(worker._http("http_request", {"method": "GET", "url": "/"}))
    second = asyncio.run(worker._http("http_request", {"method": "GET", "url": "/"}))

    assert first["success"] is True
    assert second["error"] == "ROUTE_EXHAUSTED"
    assert calls == 1
