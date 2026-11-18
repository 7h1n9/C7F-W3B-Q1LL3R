"""Unit tests for the protocol switch translation layer.

These pin the behaviour the real clients depend on: Codex CLI reads the
Responses event sequence, Claude Code reads the Messages event sequence, and
the app's own OpenAI-compatible engine reads Chat Completions.  Everything here
is pure -- no network, no server -- so a regression shows up as a diff rather
than as a hung worker turn.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from protocol_switch.stream import transform  # noqa: E402
from protocol_switch.translate import (  # noqa: E402
    CHAT,
    MESSAGES,
    RESPONSES,
    Answer,
    ToolCall,
    answer_from_chat,
    answer_from_messages,
    answer_from_responses,
    chat_response,
    decode,
    encode,
    from_chat,
    from_messages,
    from_responses,
    iter_sse_blocks,
    messages_response,
    render,
    resolve_endpoint,
    responses_response,
)


# ── endpoint resolution ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "base,wire,expected",
    [
        # The bug this guards: a full path used to be appended to again.
        ("https://relay.example/v1/chat/completions", CHAT, "https://relay.example/v1/chat/completions"),
        ("https://relay.example/v1", CHAT, "https://relay.example/v1/chat/completions"),
        # A bare host must NOT gain /v1: api.deepseek.com serves /chat/completions.
        ("https://api.deepseek.com", CHAT, "https://api.deepseek.com/chat/completions"),
        ("https://relay.example/v1/responses", RESPONSES, "https://relay.example/v1/responses"),
        ("https://relay.example/v1", RESPONSES, "https://relay.example/v1/responses"),
        # Pointing a chat upstream at the other dialect's path still resolves.
        ("https://relay.example/v1/responses", CHAT, "https://relay.example/v1/chat/completions"),
        ("https://relay.example/v1/", CHAT, "https://relay.example/v1/chat/completions"),
    ],
)
def test_resolve_endpoint_never_doubles_a_path(base: str, wire: str, expected: str) -> None:
    assert resolve_endpoint(base, wire) == expected


def test_resolve_endpoint_rejects_an_empty_base() -> None:
    with pytest.raises(ValueError):
        resolve_endpoint("   ", CHAT)


# ── request translation ─────────────────────────────────────────────────────


def test_chat_to_responses_moves_system_into_instructions() -> None:
    canonical = from_chat(
        {
            "model": "m",
            "messages": [
                {"role": "system", "content": "be terse"},
                {"role": "user", "content": "hi"},
            ],
            "max_tokens": 64,
            "temperature": 0.2,
        }
    )
    body = encode(RESPONSES, canonical)
    assert body["instructions"] == "be terse"
    assert body["input"] == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}
    ]
    assert body["max_output_tokens"] == 64
    assert body["temperature"] == 0.2
    # store=False keeps a relay from persisting every worker turn.
    assert body["store"] is False


def test_chat_tools_become_responses_function_tools() -> None:
    canonical = from_chat(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "go"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "run_shell",
                        "description": "run a command",
                        "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
                    },
                }
            ],
        }
    )
    body = encode(RESPONSES, canonical)
    assert body["tools"] == [
        {
            "type": "function",
            "name": "run_shell",
            "description": "run a command",
            "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
            "strict": False,
        }
    ]


def test_responses_to_chat_flattens_input_items() -> None:
    canonical = from_responses(
        {
            "model": "m",
            "instructions": "sys",
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]},
                {"type": "function_call", "call_id": "call_1", "name": "run_shell", "arguments": '{"cmd":"ls"}'},
                {"type": "function_call_output", "call_id": "call_1", "output": "a.txt"},
                {"type": "reasoning", "summary": []},
            ],
            "max_output_tokens": 32,
        }
    )
    body = encode(CHAT, canonical)
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["messages"][1] == {"role": "user", "content": "hello"}
    assert body["messages"][2]["tool_calls"][0]["function"] == {
        "name": "run_shell",
        "arguments": '{"cmd":"ls"}',
    }
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "call_1", "content": "a.txt"}
    # A reasoning item carries no conversation content and must not become a turn.
    assert len(body["messages"]) == 4
    assert body["max_tokens"] == 32


def test_messages_to_chat_keeps_system_and_tool_blocks() -> None:
    canonical = from_messages(
        {
            "model": "m",
            "system": [{"type": "text", "text": "you are a solver"}],
            "max_tokens": 128,
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "find the flag"}]},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "running"},
                        {"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {"cmd": "id"}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "uid=0"}
                    ],
                },
            ],
            "tools": [{"name": "bash", "description": "shell", "input_schema": {"type": "object"}}],
        }
    )
    body = encode(CHAT, canonical)
    assert body["messages"][0] == {"role": "system", "content": "you are a solver"}
    assert body["messages"][1] == {"role": "user", "content": "find the flag"}
    assistant = body["messages"][2]
    assert assistant["content"] == "running"
    assert assistant["tool_calls"][0]["id"] == "toolu_1"
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"cmd":"id"}'
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "toolu_1", "content": "uid=0"}
    assert body["tools"][0]["function"]["name"] == "bash"


def test_messages_round_trip_back_to_messages() -> None:
    canonical = from_messages(
        {"model": "m", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}
    )
    body = encode(MESSAGES, canonical)
    # max_tokens is required by the Messages API and must survive the round trip.
    assert body["max_tokens"] == 8
    assert body["messages"] == [{"role": "user", "content": "hi"}]


# ── answer parsing and rendering ────────────────────────────────────────────


def test_answer_from_chat_reads_tool_calls_and_usage() -> None:
    answer = answer_from_chat(
        {
            "id": "chatcmpl-1",
            "model": "m",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {"id": "call_9", "type": "function", "function": {"name": "bash", "arguments": "{}"}}
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7},
        }
    )
    assert answer.tool_calls[0].name == "bash"
    assert (answer.input_tokens, answer.output_tokens) == (5, 7)

    rendered = responses_response(answer)
    assert rendered["output"][0]["type"] == "function_call"
    assert rendered["output"][0]["call_id"] == "call_9"
    assert rendered["usage"] == {"input_tokens": 5, "output_tokens": 7, "total_tokens": 12}


def test_answer_from_responses_reads_output_text() -> None:
    answer = answer_from_responses(
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
            ],
            "usage": {"input_tokens": 3, "output_tokens": 4},
        }
    )
    assert answer.text == "done"
    chat = chat_response(answer)
    assert chat["choices"][0]["message"]["content"] == "done"
    assert chat["choices"][0]["finish_reason"] == "stop"


def test_answer_from_messages_maps_tool_use_to_chat_tool_calls() -> None:
    answer = answer_from_messages(
        {
            "id": "msg_1",
            "model": "m",
            "content": [{"type": "tool_use", "id": "toolu_7", "name": "bash", "input": {"cmd": "ls"}}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 1, "output_tokens": 2},
        }
    )
    assert answer.finish_reason == "tool_use"
    chat = chat_response(answer)
    assert chat["choices"][0]["finish_reason"] == "tool_calls"
    assert chat["choices"][0]["message"]["tool_calls"][0]["id"] == "toolu_7"

    back = messages_response(answer)
    assert back["stop_reason"] == "tool_use"
    assert back["content"][0]["input"] == {"cmd": "ls"}


def test_render_dispatch_covers_every_dialect() -> None:
    answer = Answer(text="hi", model="m")
    assert render(CHAT, answer)["object"] == "chat.completion"
    assert render(RESPONSES, answer)["object"] == "response"
    assert render(MESSAGES, answer)["type"] == "message"


def test_decode_rejects_an_unknown_dialect() -> None:
    with pytest.raises(ValueError):
        decode("grpc", {})


# ── streaming ───────────────────────────────────────────────────────────────

_CHAT_STREAM = [
    'data: {"id":"c1","model":"m","choices":[{"index":0,"delta":{"role":"assistant","content":"Hel"},"finish_reason":null}]}\n\n',
    'data: {"id":"c1","model":"m","choices":[{"index":0,"delta":{"content":"lo"},"finish_reason":null}]}\n\n',
    'data: {"id":"c1","model":"m","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":4,"completion_tokens":2}}\n\n',
    "data: [DONE]\n\n",
]


def _frames(dialect: str, chunks: list[str], model: str = "m") -> list[tuple[str, dict]]:
    raw = "".join(
        transform(upstream=CHAT, downstream=dialect, chunks=chunks, model=model)
    )
    out: list[tuple[str, dict]] = []
    for event, data in iter_sse_blocks([raw]):
        if data.strip() == "[DONE]":
            out.append(("[DONE]", {}))
            continue
        out.append((event, json.loads(data)))
    return out


def test_chat_stream_becomes_responses_events_for_codex() -> None:
    frames = _frames(RESPONSES, _CHAT_STREAM)
    kinds = [payload.get("type") for _, payload in frames]
    assert "response.created" in kinds
    assert "response.output_text.delta" in kinds
    assert "response.completed" in kinds
    deltas = "".join(
        payload["delta"] for _, payload in frames if payload.get("type") == "response.output_text.delta"
    )
    assert deltas == "Hello"
    completed = next(payload["response"] for _, payload in frames if payload.get("type") == "response.completed")
    assert completed["output"][0]["content"][0]["text"] == "Hello"
    assert completed["usage"] == {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6}


def test_chat_stream_becomes_messages_events_for_claude() -> None:
    frames = _frames(MESSAGES, _CHAT_STREAM)
    kinds = [payload.get("type") for _, payload in frames]
    assert kinds[0] == "message_start"
    assert "content_block_delta" in kinds
    assert kinds[-1] == "message_stop"
    text = "".join(
        payload["delta"]["text"]
        for _, payload in frames
        if payload.get("type") == "content_block_delta"
    )
    assert text == "Hello"
    delta = next(payload for _, payload in frames if payload.get("type") == "message_delta")
    assert delta["delta"]["stop_reason"] == "end_turn"


def test_chat_stream_tool_calls_become_a_responses_function_call() -> None:
    chunks = [
        'data: {"model":"m","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_a","type":"function","function":{"name":"bash","arguments":""}}]},"finish_reason":null}]}\n\n',
        'data: {"model":"m","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"cmd\\":"}}]},"finish_reason":null}]}\n\n',
        'data: {"model":"m","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"ls\\"}"}}]},"finish_reason":"tool_calls"}]}\n\n',
    ]
    frames = _frames(RESPONSES, chunks)
    added = [payload for _, payload in frames if payload.get("type") == "response.output_item.added"]
    assert added[0]["item"]["type"] == "function_call"
    assert added[0]["item"]["call_id"] == "call_a"
    arguments = "".join(
        payload["delta"]
        for _, payload in frames
        if payload.get("type") == "response.function_call_arguments.delta"
    )
    assert json.loads(arguments) == {"cmd": "ls"}
    completed = next(payload["response"] for _, payload in frames if payload.get("type") == "response.completed")
    assert completed["output"][0]["arguments"] == '{"cmd":"ls"}'


def test_chat_stream_to_chat_keeps_the_chat_dialect() -> None:
    frames = _frames(CHAT, _CHAT_STREAM)
    assert frames[-1][0] == "[DONE]"
    text = "".join(
        payload["choices"][0]["delta"].get("content") or ""
        for _, payload in frames
        if payload.get("choices")
    )
    assert text == "Hello"


def test_responses_stream_becomes_chat_chunks() -> None:
    upstream = [
        'event: response.created\ndata: {"type":"response.created","response":{"id":"r1","model":"m"}}\n\n',
        'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"Hey"}\n\n',
        'event: response.completed\ndata: {"type":"response.completed","response":{"id":"r1","model":"m","usage":{"input_tokens":1,"output_tokens":1}}}\n\n',
    ]
    raw = "".join(transform(upstream=RESPONSES, downstream=CHAT, chunks=upstream, model="m"))
    frames = [json.loads(data) for _, data in iter_sse_blocks([raw]) if data.strip() != "[DONE]"]
    text = "".join(
        payload["choices"][0]["delta"].get("content") or ""
        for payload in frames
        if payload.get("choices")
    )
    assert text == "Hey"


def test_empty_upstream_stream_still_terminates_every_dialect() -> None:
    for dialect in (CHAT, RESPONSES, MESSAGES):
        frames = _frames(dialect, [])
        assert frames, f"{dialect} produced no terminal frame"


def test_sse_block_decoder_joins_multiline_data() -> None:
    blocks = list(iter_sse_blocks(["event: x\ndata: a\ndata: b\n\n"]))
    assert blocks == [("x", "a\nb")]


@pytest.mark.parametrize("split", range(1, 30))
def test_sse_decoder_survives_a_frame_split_across_chunks(split: int) -> None:
    """A network stream never aligns to frame boundaries.

    Splitting each chunk on newlines (the obvious implementation) drops every
    frame that straddles a boundary, which shows up as a translated stream that
    stalls mid-turn.  Feeding the same bytes at every possible split pins the
    incremental parser.
    """

    raw = 'data: {"a":1}\n\ndata: {"b":2}\n\n'
    blocks = list(iter_sse_blocks([raw[:split], raw[split:]]))
    assert blocks == [("", '{"a":1}'), ("", '{"b":2}')]


def test_transform_survives_character_at_a_time_framing() -> None:
    raw = "".join(_CHAT_STREAM)
    frames = _frames(RESPONSES, list(raw))
    text = "".join(
        payload["delta"]
        for _, payload in frames
        if payload.get("type") == "response.output_text.delta"
    )
    assert text == "Hello"
