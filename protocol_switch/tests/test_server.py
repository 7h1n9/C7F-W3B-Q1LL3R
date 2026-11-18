"""End-to-end tests for the switch service.

A fake upstream (``httpx.MockTransport``) speaks Chat Completions, so the whole
service path is exercised -- routing, request translation, upstream call, SSE
translation -- without a relay.  These are the tests that would have caught the
doubled-endpoint and streaming bugs in production.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from protocol_switch.config import Settings  # noqa: E402
from protocol_switch.server import create_app  # noqa: E402
from protocol_switch.translate import iter_sse_blocks  # noqa: E402

CHAT_ANSWER = {
    "id": "chatcmpl-up",
    "object": "chat.completion",
    "created": 1,
    "model": "up-model",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "pong"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
}

CHAT_STREAM_BODY = "".join(
    [
        'data: {"id":"c1","model":"up-model","choices":[{"index":0,"delta":{"role":"assistant","content":"po"},"finish_reason":null}]}\n\n',
        'data: {"id":"c1","model":"up-model","choices":[{"index":0,"delta":{"content":"ng"},"finish_reason":null}]}\n\n',
        'data: {"id":"c1","model":"up-model","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n',
        "data: [DONE]\n\n",
    ]
)


class _Recorder:
    """Captures the last upstream request so tests can assert on the wire body."""

    def __init__(self) -> None:
        self.url = ""
        self.headers: Mapping[str, str] = {}
        self.body: dict[str, Any] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.url = str(request.url)
        self.headers = dict(request.headers)
        self.body = json.loads(request.content or b"{}")
        if self.body.get("stream"):
            # ``stream=`` (not ``content=``) is required: a MockTransport
            # response built from ``content=`` reports ``is_stream_consumed``
            # immediately, so the service's ``aiter_raw``/``aiter_text`` would
            # fail with StreamConsumed for reasons that have nothing to do with
            # the code under test.
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=httpx.ByteStream(CHAT_STREAM_BODY.encode("utf-8")),
            )
        return httpx.Response(200, json=CHAT_ANSWER)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def _client(wire: str, recorder: _Recorder, **overrides: Any) -> TestClient:
    settings = Settings(
        upstream_base_url="https://relay.example/v1",
        upstream_api_key="sk-up",
        upstream_wire=wire,
        **overrides,
    )
    return TestClient(create_app(settings, upstream_transport=recorder.transport()))


# ── buffered ────────────────────────────────────────────────────────────────


def test_responses_request_reaches_a_chat_upstream_and_answers_in_responses() -> None:
    recorder = _Recorder()
    with _client("chat_completions", recorder) as client:
        response = client.post(
            "/v1/responses",
            json={
                "model": "gpt-x",
                "instructions": "be brief",
                "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "ping"}]}],
            },
        )
    assert response.status_code == 200
    # Upstream got a Chat Completions body at the chat endpoint.
    assert recorder.url == "https://relay.example/v1/chat/completions"
    assert recorder.body["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "ping"},
    ]
    body = response.json()
    assert body["object"] == "response"
    assert body["output"][0]["content"][0]["text"] == "pong"
    assert body["usage"]["total_tokens"] == 7


def test_messages_request_reaches_a_chat_upstream_and_answers_in_messages() -> None:
    recorder = _Recorder()
    with _client("chat_completions", recorder) as client:
        response = client.post(
            "/v1/messages",
            json={
                "model": "claude-x",
                "max_tokens": 64,
                "system": "sys",
                "messages": [{"role": "user", "content": "ping"}],
            },
        )
    assert response.status_code == 200
    assert recorder.url == "https://relay.example/v1/chat/completions"
    assert recorder.body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "ping"},
    ]
    body = response.json()
    assert body["type"] == "message"
    assert body["content"] == [{"type": "text", "text": "pong"}]
    assert body["stop_reason"] == "end_turn"


def test_a_chat_upstream_is_relayed_verbatim_for_a_chat_caller() -> None:
    recorder = _Recorder()
    with _client("chat_completions", recorder) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 200
    # Same dialect both sides -> the upstream body is the caller's body, untouched.
    assert recorder.body["messages"] == [{"role": "user", "content": "hi"}]
    assert response.json() == CHAT_ANSWER


def test_a_full_endpoint_path_in_base_url_is_not_doubled() -> None:
    recorder = _Recorder()
    settings = Settings(
        upstream_base_url="https://relay.example/v1/chat/completions",
        upstream_api_key="sk-up",
        upstream_wire="chat_completions",
    )
    with TestClient(create_app(settings, upstream_transport=recorder.transport())) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 200
    assert recorder.url == "https://relay.example/v1/chat/completions"


# ── streaming ───────────────────────────────────────────────────────────────


def test_streaming_responses_emits_codex_events() -> None:
    recorder = _Recorder()
    with _client("chat_completions", recorder) as client:
        response = client.post(
            "/v1/responses",
            json={"model": "gpt-x", "input": "ping", "stream": True},
        )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    events = [json.loads(data) for _, data in iter_sse_blocks([response.text]) if data.strip()]
    kinds = [item.get("type") for item in events]
    assert kinds[0] == "response.created"
    assert "response.output_text.delta" in kinds
    assert kinds[-1] == "response.completed"
    text = "".join(
        item["delta"] for item in events if item.get("type") == "response.output_text.delta"
    )
    assert text == "pong"
    assert recorder.body["stream"] is True


def test_streaming_messages_emits_anthropic_events() -> None:
    recorder = _Recorder()
    with _client("chat_completions", recorder) as client:
        response = client.post(
            "/v1/messages",
            json={
                "model": "claude-x",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "ping"}],
                "stream": True,
            },
        )
    assert response.status_code == 200
    events = [json.loads(data) for _, data in iter_sse_blocks([response.text]) if data.strip()]
    assert events[0]["type"] == "message_start"
    assert events[-1]["type"] == "message_stop"
    text = "".join(
        item["delta"]["text"] for item in events if item.get("type") == "content_block_delta"
    )
    assert text == "pong"


def test_streaming_chat_passthrough_is_byte_identical() -> None:
    recorder = _Recorder()
    with _client("chat_completions", recorder) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        )
    assert response.status_code == 200
    assert response.text == CHAT_STREAM_BODY


# ── upstream failures and auth ──────────────────────────────────────────────


def test_upstream_error_is_rendered_in_the_callers_dialect() -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "no dispatch for this group"}})

    settings = Settings(
        upstream_base_url="https://relay.example/v1",
        upstream_api_key="sk-up",
        upstream_wire="chat_completions",
    )
    with TestClient(
        create_app(settings, upstream_transport=httpx.MockTransport(failing))
    ) as client:
        anthropic = client.post(
            "/v1/messages",
            json={"model": "m", "max_tokens": 8, "messages": [{"role": "user", "content": "x"}]},
        )
        openai = client.post(
            "/v1/responses", json={"model": "m", "input": "x"}
        )
    assert anthropic.status_code == 403
    assert anthropic.json()["type"] == "error"
    assert anthropic.json()["error"]["type"] == "permission_error"
    assert "no dispatch" in anthropic.json()["error"]["message"]
    assert openai.status_code == 403
    assert openai.json()["error"]["type"] == "permission_error"


def test_inbound_api_key_is_enforced_when_configured() -> None:
    recorder = _Recorder()
    with _client("chat_completions", recorder, inbound_api_key="secret") as client:
        denied = client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        )
        allowed = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer secret"},
            json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert denied.status_code == 401
    assert allowed.status_code == 200


def test_model_alias_rewrites_the_upstream_model() -> None:
    recorder = _Recorder()
    with _client("chat_completions", recorder, model_aliases={"gpt-x": "grok-4.5"}) as client:
        client.post(
            "/v1/chat/completions",
            json={"model": "gpt-x", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert recorder.body["model"] == "grok-4.5"


def test_health_reports_configuration_without_leaking_the_key() -> None:
    recorder = _Recorder()
    with _client("chat_completions", recorder) as client:
        payload = client.get("/health").json()
    assert payload["ok"] is True
    assert payload["upstream_wire"] == "chat_completions"
    assert payload["upstream_api_key_configured"] is True
    assert "sk-up" not in json.dumps(payload)


def test_missing_upstream_configuration_answers_503() -> None:
    with TestClient(create_app(Settings())) as client:
        response = client.post(
            "/v1/responses", json={"model": "m", "input": "x"}
        )
    assert response.status_code == 503


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/responses", "/v1/messages"])
def test_non_json_body_is_rejected(path: str) -> None:
    recorder = _Recorder()
    with _client("chat_completions", recorder) as client:
        response = client.post(path, content=b"not json", headers={"content-type": "application/json"})
    assert response.status_code == 400
