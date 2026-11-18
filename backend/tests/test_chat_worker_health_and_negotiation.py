"""A Chat-Completions Worker round must be able to say why it did nothing.

Measured live (Run ``abad0594``): four Workers on the ``openai-compatible``
engine reported ``tool_steps: 0, steps_total: 1, worker_result: FAILED`` and
nothing else.  That shape has three completely different causes -- a model that
answered in prose, an endpoint that refused the request, a parameter the model
owns -- and the event stream distinguished none of them.  Answering "why" took
database archaeology plus a log that had already been unlinked.

These tests pin the three halves of the fix:

* the Worker negotiates a model-owned parameter (measured: ``temperature`` on
  ``kimi-k3`` is allowed to be 1 and nothing else) instead of failing every turn,
* a failure carries a *named* code into the step stream and the result metadata,
* an endpoint that cannot serve a Worker turn is probed before dispatch, so it
  leaves the rotation instead of burning a round.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.solver.muteki import coordinator as coordinator_module
from app.solver.muteki.coordinator import MutekiCoordinator
from app.solver.muteki.worker import chat_health
from app.solver.muteki.worker.openai_compatible import (
    OpenAICompatibleWorker,
    provider_failure_code,
)
from app.solver.muteki.workers import EngineProfile


def _status_error(status: int, body: str = "{}") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://relay.example/v1/chat/completions")
    response = httpx.Response(status, request=request, text=body)
    return httpx.HTTPStatusError(f"{status} from relay", request=request, response=response)


def _worker(**overrides: Any) -> OpenAICompatibleWorker:
    kwargs: dict[str, Any] = {
        "graph": SimpleNamespace(snapshot=lambda: {}),
        "container": object(),
        "workspace": Path(".").resolve(),
        "worker_id": "worker-1",
        "intent_id": "intent-1",
        "target_url": "http://127.0.0.1:8080",
        "base_url": "https://relay.example/v1",
        "api_key": "secret",
        "model": "kimi-k3",
        "max_turns": 3,
        "timeout_seconds": 30,
        "cost": SimpleNamespace(
            global_tokens=lambda: {},
            snapshot=lambda: {},
        ),
        "run_id": "run-1",
    }
    kwargs.update(overrides)
    return OpenAICompatibleWorker(**kwargs)


class _FakeClient:
    """Replays a scripted sequence of outcomes for ``chat``."""

    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def chat(self, **kwargs: Any):
        self.calls.append(kwargs)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def aclose(self) -> None:
        return None


def _reply(*, tool_calls: list[Any] | None = None, content: str = "", finish: str = "stop"):
    return SimpleNamespace(
        tool_calls=list(tool_calls or []),
        content=content,
        reasoning="",
        finish_reason=finish,
    )


# ---------------------------------------------------------------------------
# Parameter negotiation
# ---------------------------------------------------------------------------


def test_a_model_owned_temperature_is_negotiated_once_and_remembered() -> None:
    worker = _worker()
    client = _FakeClient(
        [
            _status_error(400, '{"error":{"message":"field temperature invalid, only 1 is allowed for this model"}}'),
            _reply(tool_calls=[SimpleNamespace(id="1", name="http_request", arguments="{}")]),
        ]
    )

    first = asyncio.run(worker._chat_turn(client, [{"role": "user", "content": "go"}]))
    assert first.tool_calls, "the retry must return the model's real answer"
    assert client.calls[0]["temperature"] == 0.0
    assert client.calls[1]["temperature"] == 1.0

    # Remembered for the rest of the round: no third request is spent on it.
    client._outcomes.append(_reply(tool_calls=[]))
    asyncio.run(worker._chat_turn(client, [{"role": "user", "content": "go"}]))
    assert client.calls[2]["temperature"] == 1.0


def test_a_failure_that_is_not_about_one_parameter_is_not_retried() -> None:
    worker = _worker()
    client = _FakeClient([_status_error(403, '{"error":{"message":"group disabled"}}')])

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(worker._chat_turn(client, [{"role": "user", "content": "go"}]))
    assert len(client.calls) == 1, "a real outage must surface, not be retried away"


def test_the_same_refusal_twice_does_not_loop() -> None:
    worker = _worker(model="stubborn")
    refusal = _status_error(
        400, '{"error":{"message":"field temperature invalid, only 1 is allowed for this model"}}'
    )
    client = _FakeClient([refusal, _status_error(400, '{"error":{"message":"field temperature invalid, only 1 is allowed for this model"}}')])

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(worker._chat_turn(client, [{"role": "user", "content": "go"}]))
    assert len(client.calls) == 2


# ---------------------------------------------------------------------------
# Named failures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "OPENAI_COMPATIBLE_PROVIDER_DENIED"),
        (403, "OPENAI_COMPATIBLE_PROVIDER_DENIED"),
        (429, "OPENAI_COMPATIBLE_PROVIDER_RATE_LIMITED"),
        (503, "OPENAI_COMPATIBLE_PROVIDER_UNAVAILABLE"),
        (400, "OPENAI_COMPATIBLE_PROVIDER_REJECTED"),
    ],
)
def test_provider_failures_get_a_named_code(status: int, expected: str) -> None:
    assert provider_failure_code(_status_error(status)) == expected


def test_a_failure_without_an_http_cause_keeps_the_generic_code() -> None:
    assert provider_failure_code(RuntimeError("boom")) == "OPENAI_COMPATIBLE_WORKER_FAILED"


def test_a_failed_round_reports_its_code_as_an_error_step() -> None:
    steps: list[dict[str, str]] = []
    module = __import__(
        "app.solver.muteki.worker.openai_compatible", fromlist=["openai_compatible"]
    )
    client = _FakeClient([_status_error(403, '{"error":{"message":"group disabled"}}')])

    class _Patched(module.LLMClient):
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def chat(self, **kwargs: Any):
            return await client.chat(**kwargs)

        async def aclose(self) -> None:
            return None

    worker = _worker(step_callback=steps.append)
    module_original = module.LLMClient
    module.LLMClient = _Patched  # type: ignore[misc]
    try:
        result = asyncio.run(worker.run(goal="solve it", payload={}))
    finally:
        module.LLMClient = module_original  # type: ignore[misc]

    assert str(result.status) == "FAILED"
    assert result.metadata["result_code"] == "OPENAI_COMPATIBLE_PROVIDER_DENIED"
    assert "group disabled" in str(result.metadata["provider_detail"])
    assert {"kind": "error", "tool": "OPENAI_COMPATIBLE_PROVIDER_DENIED"} in [
        {"kind": item.get("kind"), "tool": item.get("tool")} for item in steps
    ]


def test_a_round_without_tool_calls_says_so() -> None:
    steps: list[dict[str, str]] = []
    module = __import__(
        "app.solver.muteki.worker.openai_compatible", fromlist=["openai_compatible"]
    )
    client = _FakeClient([_reply(content="I could not find anything.", finish="stop")])

    class _Patched(module.LLMClient):
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def chat(self, **kwargs: Any):
            return await client.chat(**kwargs)

        async def aclose(self) -> None:
            return None

    worker = _worker(step_callback=steps.append)
    module_original = module.LLMClient
    module.LLMClient = _Patched  # type: ignore[misc]
    try:
        result = asyncio.run(worker.run(goal="solve it", payload={}))
    finally:
        module.LLMClient = module_original  # type: ignore[misc]

    assert result.metadata["result_code"] == "OPENAI_COMPATIBLE_NO_TOOL_CALL"
    assert result.metadata["finish_reason"] == "stop"
    assert "content_chars=26" in str(result.metadata["provider_detail"])
    assert {"kind": "note", "tool": "NO_TOOL_CALL"} in [
        {"kind": item.get("kind"), "tool": item.get("tool")} for item in steps
    ]


# ---------------------------------------------------------------------------
# Endpoint health probe
# ---------------------------------------------------------------------------


class _Graph:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit_event(self, *, actor: str, event_type: Any, payload: dict[str, Any]) -> None:
        self.events.append({"actor": actor, "type": str(event_type), **payload})

    def engine_checks(self) -> list[dict[str, Any]]:
        return [event for event in self.events if "engine.checked" in event["type"]]

    def worker_step_events(self) -> list[dict[str, Any]]:
        return [event for event in self.events if event["type"] == "worker_step"]


def _profile(**overrides: Any) -> EngineProfile:
    driver = {
        "protocol": "chat_completions",
        "base_url": "https://relay.example/v1",
        "model": "kimi-k3",
        "wire_api": "chat_completions",
        "api_key_ref": "env:OPENAI_API_KEY",
    }
    driver.update(overrides.pop("driver", {}))
    environment = overrides.pop("environment", {"OPENAI_API_KEY": "secret"})
    return EngineProfile(
        "openai-compatible:abc",
        environment=environment,
        driver_profile=driver,
        **overrides,
    )


def _patch_probe(monkeypatch: pytest.MonkeyPatch, outcome: tuple[bool, str]) -> None:
    async def _fake(_profile: Any, **_kwargs: Any):
        return outcome

    # The Coordinator binds the probe at import time, so the module attribute
    # is the one that has to change.
    monkeypatch.setattr(coordinator_module, "probe_chat_worker", _fake)


def test_a_chat_profile_is_probed_before_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_probe(monkeypatch, (False, "MODEL_PERMISSION_DENIED"))
    coordinator = object.__new__(MutekiCoordinator)
    coordinator.graph = _Graph()
    coordinator._unavailable_engine_ids = set()

    profile = _profile()
    asyncio.run(coordinator._probe_chat_profiles([profile]))

    checks = coordinator.graph.engine_checks()
    assert [event["reason_code"] for event in checks] == ["MODEL_PERMISSION_DENIED"]
    assert checks[0]["source"] == "worker_chat_endpoint"
    assert profile.engine_id in coordinator._unavailable_engine_ids


def test_a_recovered_chat_profile_returns_to_rotation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _healthy(*_a: Any, **_k: Any):
        return True, "HEALTHY"

    monkeypatch.setattr(coordinator_module, "probe_chat_worker", _healthy)
    coordinator = object.__new__(MutekiCoordinator)
    coordinator.graph = _Graph()
    coordinator._unavailable_engine_ids = {"openai-compatible:abc"}

    asyncio.run(coordinator._probe_chat_profiles([_profile()], retry=True))

    assert coordinator._unavailable_engine_ids == set()
    assert coordinator.graph.engine_checks()[0]["reactivated"] is True


def test_the_probe_reads_the_profile_credential() -> None:
    profile = _profile(
        driver={"api_key_ref": "env:OTHER_KEY"},
        environment={"OTHER_KEY": "abc123"},
    )
    assert chat_health.profile_api_key(profile) == "abc123"


def test_probe_fails_closed_when_the_profile_cannot_authenticate() -> None:
    profile = _profile(environment={})
    healthy, reason = asyncio.run(chat_health.probe_chat_worker(profile))
    assert (healthy, reason) == (False, "WORKER_PROFILE_INCOMPLETE")


def _patch_llm_client(monkeypatch: pytest.MonkeyPatch, outcomes: list[Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    class _Client:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def chat(self, **kwargs: Any):
            calls.append(kwargs)
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(chat_health, "LLMClient", _Client)
    return calls


def test_probe_reports_a_denied_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_llm_client(monkeypatch, [_status_error(403, '{"error":{"message":"denied"}}')])

    healthy, reason = asyncio.run(chat_health.probe_chat_worker(_profile()))

    assert (healthy, reason) == (False, "MODEL_PERMISSION_DENIED")


def test_probe_reports_an_unavailable_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_llm_client(monkeypatch, [_status_error(503, "Service temporarily unavailable")])

    healthy, reason = asyncio.run(chat_health.probe_chat_worker(_profile()))

    assert (healthy, reason) == (False, "MODEL_UNAVAILABLE")


def test_probe_negotiates_a_refused_temperature(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_llm_client(
        monkeypatch,
        [
            _status_error(400, '{"error":{"message":"field temperature invalid, only 1 is allowed for this model"}}'),
            _reply(tool_calls=[SimpleNamespace(id="1", name="probe", arguments="{}")]),
        ],
    )

    assert asyncio.run(chat_health.probe_chat_worker(_profile())) == (True, "HEALTHY")
    assert calls[0]["temperature"] == 0.0
    assert calls[1]["temperature"] == 1.0


def test_probe_is_honest_about_an_answer_without_a_tool_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reachable but unproven is *reported*, not failed: prose is legal."""

    _patch_llm_client(monkeypatch, [_reply(content="ok")])

    assert asyncio.run(chat_health.probe_chat_worker(_profile())) == (
        True,
        "WORKER_TOOLCALL_UNCONFIRMED",
    )


# ---------------------------------------------------------------------------
# Worker outcome code reaches the event stream
# ---------------------------------------------------------------------------


def test_worker_finished_carries_the_named_result_code() -> None:
    coordinator = object.__new__(MutekiCoordinator)
    coordinator.graph = _Graph()
    coordinator._worker_step_state = {}
    coordinator._worker_step_lock = threading.Lock()

    coordinator._record_worker_step(
        {"worker_id": "w-1", "kind": "note", "tool": "NO_TOOL_CALL", "session": ""}
    )
    coordinator._flush_worker_steps(
        "w-1",
        result="FAILED",
        result_code="OPENAI_COMPATIBLE_NO_TOOL_CALL",
    )

    finished = coordinator.graph.worker_step_events()[-1]
    assert finished["worker_result"] == "FAILED"
    assert finished["worker_result_code"] == "OPENAI_COMPATIBLE_NO_TOOL_CALL"
