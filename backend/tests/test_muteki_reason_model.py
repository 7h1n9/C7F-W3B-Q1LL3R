import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.engines.openai_compatible import ModelProviderError, OpenAICompatibleEngine
from app.solver.muteki.adapter.reason_model import CoordinatorReasonModel


class _FakeEngine:
    last_trace = {"input_tokens": 11, "output_tokens": 7}

    async def next_contract(self, messages, schema, *, name):
        assert messages and messages[0]["role"] == "system"
        assert schema["properties"]["intents"]["maxItems"] == 4
        assert name == "muteki_coordinator_reason"
        return {
            "verdict": "explore",
            "goal_met": False,
            "complete_why": "",
            "drift": "",
            "intents": [
                {
                    "id": "I1",
                    "goal": "inspect the authenticated business endpoint",
                    "worker_class": "code",
                    "rationale": "The Blackboard contains a verified route fact.",
                    "route_hash": "web:idor:business",
                    "branch_id": "",
                    "lane_key": "",
                    "risk_class": "",
                    "resource_key": "",
                    "depends_on": [],
                    "from": [1],
                    "payload": {"tool_name": "http_session_request", "arguments": {"method": "GET"}},
                }
            ],
        }

    async def close(self):
        return None


def test_coordinator_reason_model_returns_typed_intents() -> None:
    recorded = []

    async def record(trace):
        recorded.append(trace)

    provider = CoordinatorReasonModel(
        _FakeEngine(),
        model_config_id="reason-1",
        model_name="deepseek-reasoner",
        usage_recorder=record,
    )
    result = asyncio.run(provider({"challenge_id": "run-1", "revision": 1, "facts": [], "intents": [], "dead_ends": [], "flags": []}))
    assert result.verdict == "explore"
    assert result[0]["payload"]["tool_name"] == "http_session_request"
    assert recorded == [{"input_tokens": 11, "output_tokens": 7}]


def test_coordinator_reason_model_falls_back_without_exposing_provider_error() -> None:
    class BrokenEngine(_FakeEngine):
        async def next_contract(self, messages, schema, *, name):
            raise RuntimeError("provider response contained a secret")

    def fallback(_snapshot):
        return [{"goal": "bounded local fallback", "payload": {"tool_name": "http_request"}}]
    provider = CoordinatorReasonModel(
        BrokenEngine(),
        model_config_id="reason-2",
        model_name="step-reasoner",
        fallback=fallback,
    )
    result = asyncio.run(provider({"facts": [], "intents": [], "dead_ends": [], "flags": []}))
    assert result[0]["goal"] == "bounded local fallback"
    assert "secret" not in provider.last_error_code.casefold()


def test_coordinator_reason_model_clears_error_after_later_success() -> None:
    class FlakyEngine(_FakeEngine):
        def __init__(self):
            self.calls = 0

        async def next_contract(self, messages, schema, *, name):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("MODEL_UNAVAILABLE")
            return await super().next_contract(messages, schema, name=name)

    provider = CoordinatorReasonModel(
        FlakyEngine(),
        model_config_id="reason-flaky",
        model_name="reasoner",
        fallback=lambda _snapshot: [],
    )
    snapshot = {"facts": [], "intents": [], "dead_ends": [], "flags": []}
    asyncio.run(provider(snapshot))
    assert provider.last_error_code == "MODEL_UNAVAILABLE"
    result = asyncio.run(provider(snapshot))
    assert result
    assert provider.last_error_code == ""


def test_coordinator_reason_model_uses_official_plain_chat_contract() -> None:
    calls = []

    class OfficialReasonEngine:
        last_trace = {"input_tokens": 101, "output_tokens": 23}

        async def chat(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                content=json.dumps(
                    {
                        "verdict": "explore",
                        "goal_met": False,
                        "complete_why": "",
                        "drift": "",
                        "intents": [
                            {
                                "id": "I1",
                                "from": [3],
                                "goal": "inspect the authenticated business endpoint",
                                "worker_class": "code",
                                "route_hash": "web:idor:business",
                                "rationale": "Use the verified endpoint fact.",
                            }
                        ],
                        "pinned_facts": [3],
                        "audit": [],
                    }
                )
            )

        async def close(self):
            return None

    provider = CoordinatorReasonModel(
        OfficialReasonEngine(),
        model_config_id="reason-official",
        model_name="deepseek-v4-flash",
        run_id="run-1",
        challenge_id="challenge-1",
    )
    result = asyncio.run(
        provider(
            {
                "run_id": "run-1",
                "challenge_id": "challenge-1",
                "revision": 3,
                "facts": [],
                "intents": [],
                "dead_ends": [],
                "flags": [],
            }
        )
    )

    assert result.verdict == "explore"
    assert result[0]["goal"] == "inspect the authenticated business endpoint"
    assert result[0]["payload"]["from_facts"] == [3]
    assert calls[0]["model"] == "deepseek-v4-flash"
    assert calls[0]["max_tokens"] is None
    assert calls[0]["stream"] is False
    assert "response_format" not in calls[0]


def test_openai_engine_chat_matches_official_reason_request(monkeypatch) -> None:
    requests = []

    class Response:
        headers = {}

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "model": "deepseek-v4-flash",
                "choices": [{"message": {"content": '{"verdict":"explore","intents":[]}'}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 8},
            }

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def post(self, _url, **kwargs):
            requests.append(kwargs["json"])
            return Response()

        async def aclose(self):
            return None

    monkeypatch.setattr("app.engines.openai_compatible.httpx.AsyncClient", Client)
    engine = OpenAICompatibleEngine("http://provider.test/v1", "secret", "worker-model")
    response = asyncio.run(
        engine.chat(
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": "plan"}],
            temperature=0.3,
            max_tokens=None,
            stream=False,
        )
    )
    assert response.content.startswith("{\"verdict\"")
    assert requests[0]["model"] == "deepseek-v4-flash"
    assert requests[0]["stream"] is False
    assert "max_tokens" not in requests[0]
    assert "response_format" not in requests[0]


@pytest.mark.asyncio
async def test_openai_engine_reason_400_is_not_reported_as_unavailable(monkeypatch) -> None:
    class Response:
        status_code = 400
        headers = {}

        def raise_for_status(self):
            raise httpx.HTTPStatusError(
                "bad request",
                request=httpx.Request("POST", "https://provider.test/v1/chat/completions"),
                response=httpx.Response(400),
            )

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def post(self, _url, **_kwargs):
            return Response()

        async def aclose(self):
            return None

    monkeypatch.setattr("app.engines.openai_compatible.httpx.AsyncClient", Client)
    engine = OpenAICompatibleEngine("http://provider.test/v1", "secret", "model")
    with pytest.raises(ModelProviderError) as error_info:
        await engine.chat(
            model="model",
            messages=[{"role": "user", "content": "health"}],
            max_tokens=16,
        )
    assert error_info.value.code == "MODEL_BAD_REQUEST"


def test_coordinator_reason_health_reactivation_clears_transient_error() -> None:
    class RecoveringReasonEngine:
        last_trace = {"input_tokens": 3, "output_tokens": 1}

        async def chat(self, **kwargs):
            assert kwargs["solver_id"] == "reason-health"
            return SimpleNamespace(content="OK")

        async def close(self):
            return None

    recorded = []

    async def record(trace):
        recorded.append(trace)

    provider = CoordinatorReasonModel(
        RecoveringReasonEngine(),
        model_config_id="reason-health",
        model_name="reasoner",
        usage_recorder=record,
    )
    provider.last_error_code = "MODEL_UNAVAILABLE"
    assert provider.needs_reactivation is True
    healthy, reason = asyncio.run(provider.health())
    assert (healthy, reason) == (True, "HEALTHY")
    assert provider.last_error_code == ""
    assert provider.needs_reactivation is False
    assert recorded[0]["call_type"] == "reason_health"


def test_coordinator_reason_health_does_not_reactivate_contract_failure() -> None:
    class BrokenReasonEngine:
        async def chat(self, **_kwargs):
            raise RuntimeError("MODEL_BAD_REQUEST")

    provider = CoordinatorReasonModel(
        BrokenReasonEngine(),
        model_config_id="reason-contract",
        model_name="reasoner",
    )
    provider.last_error_code = "MODEL_BAD_REQUEST"
    assert provider.needs_reactivation is False
