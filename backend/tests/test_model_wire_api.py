"""Tests for the OpenAI wire-dialect support (Chat Completions vs Responses).

Two classes of bug are pinned here:

* an operator's ``base_url`` being appended to blindly, which produced
  ``/v1/chat/completions/chat/completions``; and
* a Responses-only relay being sent a Chat body (or the reverse), which surfaces
  as a 404/502 that looks like an outage rather than a misconfiguration.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.engines.wire import (
    CHAT_COMPLETIONS,
    RESPONSES,
    normalize_wire_api,
    resolve_endpoint,
    responses_to_chat_body,
    to_responses_request,
)
from app.schemas.model_config import ModelConfigWrite
from muteki.core.llm import LLMClient


# ── endpoint resolution ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "base,wire,expected",
    [
        # The regression: a full path used to be appended to again.
        ("https://relay.example/v1/chat/completions", CHAT_COMPLETIONS, "https://relay.example/v1/chat/completions"),
        ("https://relay.example/v1", CHAT_COMPLETIONS, "https://relay.example/v1/chat/completions"),
        # A bare host keeps its shape: api.deepseek.com serves /chat/completions,
        # so inserting /v1 would break a configuration that works today.
        ("https://api.deepseek.com", CHAT_COMPLETIONS, "https://api.deepseek.com/chat/completions"),
        ("https://relay.example/v1", RESPONSES, "https://relay.example/v1/responses"),
        ("https://relay.example/v1/responses", RESPONSES, "https://relay.example/v1/responses"),
        # Switching dialect on a base_url that carried the other endpoint's path.
        ("https://relay.example/v1/chat/completions", RESPONSES, "https://relay.example/v1/responses"),
        ("https://relay.example/v1/responses", CHAT_COMPLETIONS, "https://relay.example/v1/chat/completions"),
        ("https://relay.example/v1/", CHAT_COMPLETIONS, "https://relay.example/v1/chat/completions"),
    ],
)
def test_resolve_endpoint_never_doubles_a_path(base: str, wire: str, expected: str) -> None:
    assert resolve_endpoint(base, wire) == expected


def test_resolve_endpoint_rejects_an_empty_base() -> None:
    with pytest.raises(ValueError):
        resolve_endpoint("   ", CHAT_COMPLETIONS)


def test_normalize_wire_api_defaults_unknown_values() -> None:
    assert normalize_wire_api(None) == CHAT_COMPLETIONS
    assert normalize_wire_api("") == CHAT_COMPLETIONS
    assert normalize_wire_api("RESPONSES") == RESPONSES
    assert normalize_wire_api("chat") == CHAT_COMPLETIONS
    assert normalize_wire_api("nonsense") == CHAT_COMPLETIONS


# ── request / response translation ──────────────────────────────────────────


def test_chat_request_becomes_a_responses_request() -> None:
    body = to_responses_request(
        {
            "model": "m",
            "messages": [
                {"role": "system", "content": "be terse"},
                {"role": "user", "content": "hi"},
            ],
            "max_tokens": 64,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
    )
    assert body["instructions"] == "be terse"
    assert body["input"] == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}
    ]
    assert body["max_output_tokens"] == 64
    assert body["temperature"] == 0.2
    assert body["text"] == {"format": {"type": "json_object"}}
    # A relay must not persist every worker turn.
    assert body["store"] is False
    # The engine reads the whole JSON body, so asking for a stream would break it.
    assert "stream" not in body


def test_chat_tools_and_tool_results_survive_the_round_trip() -> None:
    body = to_responses_request(
        {
            "model": "m",
            "messages": [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "run_shell", "arguments": '{"cmd":"ls"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "a.txt"},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "run_shell",
                        "description": "run",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        }
    )
    assert body["input"][0] == {
        "type": "function_call",
        "call_id": "call_1",
        "name": "run_shell",
        "arguments": '{"cmd":"ls"}',
    }
    assert body["input"][1] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "a.txt",
    }
    assert body["tools"][0]["name"] == "run_shell"


def test_responses_reply_becomes_a_chat_reply() -> None:
    chat = responses_to_chat_body(
        {
            "id": "resp_1",
            "model": "m",
            "output": [
                {"type": "reasoning", "summary": []},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "done"}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_9",
                    "name": "run_shell",
                    "arguments": '{"cmd":"id"}',
                },
            ],
            "usage": {"input_tokens": 3, "output_tokens": 4},
        }
    )
    choice = chat["choices"][0]
    assert choice["message"]["content"] == "done"
    assert choice["message"]["tool_calls"][0]["id"] == "call_9"
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "run_shell"
    assert choice["finish_reason"] == "tool_calls"
    assert chat["usage"] == {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}


# ── schema contract ─────────────────────────────────────────────────────────


def test_openai_wire_provider_defaults_to_chat_completions() -> None:
    config = ModelConfigWrite(
        name="relay",
        provider_type="openai_compatible",
        base_url="https://relay.example/v1",
        model_name="m",
        api_key="sk",
        roles=["worker"],
    )
    assert config.wire_api == CHAT_COMPLETIONS


def test_openai_wire_provider_can_pin_responses() -> None:
    config = ModelConfigWrite(
        name="relay",
        provider_type="openai_compatible",
        base_url="https://relay.example/v1",
        model_name="m",
        api_key="sk",
        roles=["worker"],
        wire_api="responses",
    )
    assert config.wire_api == RESPONSES


def test_an_unknown_wire_api_falls_back_instead_of_failing() -> None:
    # A legacy/typo'd stored value must not 422 an otherwise valid save.
    config = ModelConfigWrite.model_construct(
        name="relay",
        provider_type="openai_compatible",
        base_url="https://relay.example/v1",
        model_name="m",
        api_key="sk",
        roles=["worker"],
        wire_api=None,
    )
    assert config.validate_provider_contract().wire_api == CHAT_COMPLETIONS


def test_codex_stays_pinned_to_responses() -> None:
    config = ModelConfigWrite(
        name="codex",
        provider_type="codex_cli",
        base_url="https://relay.example/v1",
        model_name="gpt-5.6-sol",
        api_key="sk",
        roles=["worker"],
        wire_api="chat_completions",
    )
    assert config.wire_api == RESPONSES


def test_a_cli_provider_never_carries_a_wire_dialect() -> None:
    config = ModelConfigWrite(
        name="claude",
        provider_type="claude_cli",
        base_url="https://relay.example/v1",
        model_name="m",
        api_key="sk",
        roles=["worker"],
        wire_api="responses",
    )
    assert config.wire_api is None


# ── LLMClient routing ───────────────────────────────────────────────────────

_RESPONSES_BODY = {
    "id": "resp_1",
    "object": "response",
    "model": "m",
    "status": "completed",
    "output": [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "pong"}],
        }
    ],
    "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
}

_RESPONSES_SSE = "".join(
    [
        'event: response.created\ndata: {"type":"response.created","response":{"id":"r1","model":"m"}}\n\n',
        'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"po"}\n\n',
        'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"ng"}\n\n',
        'event: response.completed\ndata: {"type":"response.completed","response":{"id":"r1","model":"m","usage":{"input_tokens":2,"output_tokens":3}}}\n\n',
    ]
)


_CHAT_BODY = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "model": "m",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "pong"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
}


class _Recorder:
    """Fake relay that answers each dialect on its own path."""

    def __init__(self, *, streaming: bool = False) -> None:
        self.url = ""
        self.body: dict = {}
        self.streaming = streaming

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.url = str(request.url)
        self.body = json.loads(request.content or b"{}")
        responses_side = self.url.endswith("/responses")
        if self.streaming:
            if not responses_side:
                raise AssertionError(f"unexpected streaming target: {self.url}")
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=httpx.ByteStream(_RESPONSES_SSE.encode("utf-8")),
            )
        return httpx.Response(200, json=_RESPONSES_BODY if responses_side else _CHAT_BODY)


def _client(recorder: _Recorder, wire_api: str) -> LLMClient:
    client = LLMClient(api_key="sk", base_url="https://relay.example/v1", wire_api=wire_api)
    # The client builds its own AsyncClient; swap in a transport-bound one so the
    # routing can be asserted without a live relay.
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler))
    return client


def test_llm_client_chat_mode_hits_chat_completions() -> None:
    recorder = _Recorder()

    async def run() -> None:
        client = _client(recorder, CHAT_COMPLETIONS)
        try:
            response = await client.chat(
                model="m", messages=[{"role": "user", "content": "hi"}], stream=False
            )
        finally:
            await client.aclose()
        assert response.content == "pong"

    asyncio.run(run())
    assert recorder.url == "https://relay.example/v1/chat/completions"
    assert recorder.body["messages"] == [{"role": "user", "content": "hi"}]


def test_llm_client_responses_mode_translates_both_ways() -> None:
    recorder = _Recorder()

    async def run() -> None:
        client = _client(recorder, RESPONSES)
        try:
            response = await client.chat(
                model="m",
                messages=[
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": "hi"},
                ],
                stream=False,
            )
        finally:
            await client.aclose()
        assert response.content == "pong"
        assert (response.input_tokens, response.output_tokens) == (2, 3)

    asyncio.run(run())
    assert recorder.url == "https://relay.example/v1/responses"
    # The upstream got a Responses body, not the Chat one.
    assert recorder.body["instructions"] == "sys"
    assert recorder.body["input"][0]["content"][0]["text"] == "hi"
    assert "messages" not in recorder.body


def test_llm_client_responses_streaming_folds_events_into_chat_chunks() -> None:
    recorder = _Recorder(streaming=True)

    async def run() -> str:
        client = _client(recorder, RESPONSES)
        try:
            response = await client.chat(
                model="m",
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
            )
        finally:
            await client.aclose()
        return response.content

    assert asyncio.run(run()) == "pong"
    assert recorder.url == "https://relay.example/v1/responses"


def test_llm_client_iter_chat_deltas_streams_through_the_responses_wire() -> None:
    recorder = _Recorder(streaming=True)

    async def run() -> list[str]:
        client = _client(recorder, RESPONSES)
        try:
            return [
                chunk
                async for chunk in client.iter_chat_deltas(
                    model="m", messages=[{"role": "user", "content": "hi"}]
                )
            ]
        finally:
            await client.aclose()

    assert "".join(asyncio.run(run())) == "pong"
    assert recorder.url == "https://relay.example/v1/responses"
