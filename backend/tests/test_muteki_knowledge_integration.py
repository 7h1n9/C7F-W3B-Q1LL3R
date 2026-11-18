import asyncio
import json
from types import SimpleNamespace

import pytest

from app.solver.muteki.adapter.reason_model import CoordinatorReasonModel
from app.solver.muteki.runtime import muteki_runtime as muteki_runtime_module
from app.solver.muteki.runtime.muteki_runtime import MutekiRuntime


def test_coordinator_reason_prompt_marks_knowledge_as_unverified_prior() -> None:
    calls: list[dict] = []

    class FakeReasonEngine:
        last_trace = {"input_tokens": 9, "output_tokens": 4}

        async def chat(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                content=json.dumps(
                    {
                        "verdict": "explore",
                        "goal_met": False,
                        "complete_why": "",
                        "drift": "",
                        "intents": [],
                    }
                )
            )

        async def close(self) -> None:
            return None

    provider = CoordinatorReasonModel(
        FakeReasonEngine(),
        model_config_id="reason-1",
        model_name="reasoner",
        knowledge_prior=(
            "[sqli-boolean-blind] Boolean SQL injection\n"
            "Validate the parameter with a controlled boolean oracle."
        ),
    )

    asyncio.run(
        provider(
            {
                "challenge_id": "run-1",
                "revision": 1,
                "facts": [],
                "intents": [],
                "dead_ends": [],
                "flags": [],
            }
        )
    )

    prompt = str(calls[0]["messages"][1]["content"])
    assert "## Unverified knowledge prior" in prompt
    assert "not graph evidence and is not a VerifiedFact" in prompt
    assert "[sqli-boolean-blind]" in prompt
    assert "Independently validate every claim" in prompt


@pytest.mark.asyncio
async def test_muteki_runtime_passes_one_run_scoped_prior_to_reason(monkeypatch) -> None:
    runtime = MutekiRuntime.__new__(MutekiRuntime)
    runtime._run_id = "run-1"
    runtime.run = SimpleNamespace(
        hints_json={"knowledge_mode": "on"},
        engine_type="openai_compatible",
        model_config_id="worker-1",
    )
    runtime.challenge = SimpleNamespace(
        id="challenge-1",
        mode="ctf",
        goal="",
    )
    runtime._reason_provider = lambda _snapshot: []
    runtime._reason_model = None
    runtime._graph = None

    config = SimpleNamespace(
        id="reason-1",
        name="reason",
        enabled=True,
        provider_type="openai_compatible",
        encrypted_api_key="encrypted",
        base_url="https://reason.example/v1",
        model_name="reasoner",
        request_timeout_seconds=30,
        action_protocol="json_schema",
        max_output_tokens=2048,
        temperature=0.0,
        max_retries=0,
        retry_base_seconds=1.0,
        rate_limit_cooldown_seconds=60,
    )

    class FakeSession:
        async def get(self, _model, _identifier):
            return config

    runtime.session = FakeSession()
    captured: dict[str, object] = {}

    class FakeReasonModel:
        def __init__(self, _engine, **kwargs):
            captured.update(kwargs)

        async def close(self) -> None:
            return None

    prior_context = {
        "mode": "on",
        "enabled": True,
        "formatted_prior": "[sqli-prior] Boolean SQL injection",
        "results": [{"item": {"id": "sqli-prior"}}],
        "searched_at": "2026-09-13T00:00:00+00:00",
    }
    build_calls: list[tuple[object, object]] = []

    def fake_build(run, challenge):
        build_calls.append((run, challenge))
        return prior_context

    monkeypatch.setattr(
        muteki_runtime_module,
        "runtime_selection_from_hints",
        lambda *_args, **_kwargs: ("reason-1", []),
    )
    monkeypatch.setattr(muteki_runtime_module, "decrypt_api_key", lambda _value: "api-key")
    monkeypatch.setattr(
        muteki_runtime_module.knowledge_context_service,
        "build",
        fake_build,
    )
    monkeypatch.setattr(
        muteki_runtime_module,
        "OpenAICompatibleEngine",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        muteki_runtime_module,
        "CoordinatorReasonModel",
        FakeReasonModel,
    )

    provider = await runtime._build_reason_provider()

    assert isinstance(provider, FakeReasonModel)
    assert build_calls == [(runtime.run, runtime.challenge)]
    assert captured["knowledge_prior"] == prior_context["formatted_prior"]


@pytest.mark.asyncio
async def test_openai_worker_turn_receives_one_intent_scoped_prior(monkeypatch) -> None:
    runtime = MutekiRuntime.__new__(MutekiRuntime)
    runtime._run_id = "run-1"
    runtime.run = SimpleNamespace(hints_json={"knowledge_mode": "on"}, current_phase="EXECUTING")
    runtime.challenge = SimpleNamespace(id="challenge-1")
    runtime._graph = _RecordingGraph()
    runtime._worker_models = {}
    runtime.session = _StubSession()

    build_calls: list[dict[str, object]] = []

    def fake_worker_prior(run, challenge, **kwargs):
        build_calls.append(kwargs)
        return {
            "mode": "on",
            "enabled": True,
            "prior": "[sqli-boolean-blind] Confirm the parameter with a boolean oracle",
            "item_ids": ["sqli-boolean-blind"],
            "searched_at": "2026-09-13T00:00:00+00:00",
        }

    monkeypatch.setattr(
        muteki_runtime_module.knowledge_context_service,
        "build_worker_prior",
        fake_worker_prior,
    )
    monkeypatch.setattr(muteki_runtime_module, "decrypt_api_key", lambda _value: "api-key")
    monkeypatch.setattr(
        muteki_runtime_module,
        "OpenAICompatibleEngine",
        lambda *_args, **_kwargs: _RecordingEngine(),
    )

    job = SimpleNamespace(
        worker_id="w1",
        intent_id="intent-1",
        goal="Probe the search parameter for boolean SQL injection",
        role="explore",
        payload={"tool_name": "http_request"},
        environment={"MUTEKI_MODEL_CONFIG_ID": "worker-1"},
    )

    first = await runtime._openai_worker_action(job, "http_request", {"url": "http://target.test/search"})
    second = await runtime._openai_worker_action(job, "http_request", {"url": "http://target.test/search"})

    assert first == ("http_request", {"url": "http://target.test/search"})
    assert second == first
    # One retrieval per Intent, not one per Tool-action refinement.
    assert len(build_calls) == 1
    assert build_calls[0]["intent_goal"] == job.goal
    assert build_calls[0]["tool_name"] == "http_request"
    engine = runtime._worker_models["worker-1"]
    assert "[sqli-boolean-blind]" in engine.messages[1]["content"]
    assert engine.messages[1]["content"].count("[sqli-boolean-blind]") == 1
    assert any(
        event["event_type"] == "runtime.worker.knowledge_prior" for event in runtime._graph.events
    )


class _RecordingGraph:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit_event(self, *, actor: str, event_type: str, payload: dict) -> None:
        self.events.append({"actor": actor, "event_type": event_type, "payload": payload})


class _StubSession:
    async def get(self, _model, _identifier):
        return SimpleNamespace(
            id="worker-1",
            enabled=True,
            provider_type="openai_compatible",
            encrypted_api_key="encrypted",
            base_url="https://worker.example/v1",
            model_name="worker-model",
            request_timeout_seconds=30,
            action_protocol="json_schema",
            max_output_tokens=2048,
            temperature=0.0,
            max_retries=0,
            retry_base_seconds=1.0,
            rate_limit_cooldown_seconds=60,
        )


class _RecordingEngine:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def next_action(self, messages):
        self.messages = messages
        return SimpleNamespace(
            type="tool",
            tool_name="http_request",
            arguments={"url": "http://target.test/search"},
        )
