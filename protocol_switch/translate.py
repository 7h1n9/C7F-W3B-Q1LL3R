"""Wire-protocol translation between Chat Completions, Responses and Messages.

The switch service accepts three inbound dialects and can forward to either an
OpenAI **Chat Completions** or an OpenAI **Responses** upstream.  Rather than
writing N x M pairwise converters, every dialect is normalised into one small
canonical representation (:class:`Turn` / :class:`Params`) and rendered back out
into whichever dialect the caller (or the upstream) speaks.

Design rules that the tests pin down:

* Nothing here performs I/O.  Translation is a pure function of the payload, so
  it can be unit-tested without a network and reused by both the streaming and
  the buffered code paths.
* Unknown/extra fields are dropped rather than forwarded.  A relay that rejects
  an unexpected key would otherwise fail for a reason that has nothing to do
  with the operator's configuration.
* Tool calls round-trip losslessly: an id assigned by the upstream is preserved
  so the caller's tool-result turn can still be matched back to its call.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterable, AsyncIterator, Iterable, Iterator, Mapping

CHAT = "chat_completions"
RESPONSES = "responses"
MESSAGES = "messages"
WIRES = (CHAT, RESPONSES)


# ── canonical representation ────────────────────────────────────────────────


@dataclass
class ToolCall:
    """One assistant-requested tool invocation."""

    id: str
    name: str
    #: JSON-encoded arguments.  Kept as the wire string (not a dict) so a
    #: partially-streamed fragment can be carried without re-parsing.
    arguments: str = "{}"

    def parsed_arguments(self) -> dict[str, Any]:
        try:
            value = json.loads(self.arguments or "{}")
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}


@dataclass
class Turn:
    """One canonical conversation turn."""

    role: str
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str = ""
    name: str = ""


@dataclass
class ToolSpec:
    name: str
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass
class Params:
    model: str = ""
    max_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    stream: bool = False
    stop: list[str] = field(default_factory=list)
    tools: list[ToolSpec] = field(default_factory=list)
    tool_choice: Any = None
    #: Canonical structured-output request:
    #: ``{"type": "json_schema", "name": ..., "schema": ..., "strict": ...}``
    #: or ``{"type": "json_object"}``.
    response_format: dict[str, Any] | None = None


@dataclass
class Canonical:
    params: Params
    turns: list[Turn]

    def system_text(self) -> str:
        return "\n\n".join(
            turn.content for turn in self.turns if turn.role == "system" and turn.content
        )


# ── small shared helpers ────────────────────────────────────────────────────


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _as_text(value: Any) -> str:
    """Flatten any dialect's content payload into plain text.

    Handles ``str``, a list of content parts, and the Responses API's
    ``{"type": "input_text"|"output_text"|"text", "text": ...}`` blocks.
    """

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("text", "content", "output"):
            if key in value:
                return _as_text(value[key])
        return ""
    if isinstance(value, (list, tuple)):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping):
                kind = str(item.get("type") or "")
                if kind in {"tool_use", "tool_result", "function_call", "function_call_output"}:
                    # Tool traffic is carried by dedicated fields, not prose.
                    continue
                text = _as_text(item)
                if text:
                    parts.append(text)
        return "".join(parts)
    return str(value)


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json_dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return "{}"


def _strip_unsupported(schema: Any) -> Any:
    """Drop JSON-Schema keywords some relays reject on a strict schema."""

    if isinstance(schema, Mapping):
        return {
            key: _strip_unsupported(item)
            for key, item in schema.items()
            if key not in {"$schema", "additionalProperties"}
        }
    if isinstance(schema, list):
        return [_strip_unsupported(item) for item in schema]
    return schema


# ── inbound: Chat Completions ───────────────────────────────────────────────


def _tool_spec_from_chat(raw: Mapping[str, Any]) -> ToolSpec | None:
    if str(raw.get("type") or "function") != "function":
        return None
    fn = raw.get("function") if isinstance(raw.get("function"), Mapping) else raw
    name = str(fn.get("name") or "").strip()
    if not name:
        return None
    params = fn.get("parameters") if isinstance(fn.get("parameters"), Mapping) else {}
    return ToolSpec(name, str(fn.get("description") or ""), dict(params))


def _turn_from_chat_message(raw: Mapping[str, Any]) -> Turn:
    role = str(raw.get("role") or "user")
    calls: list[ToolCall] = []
    for item in raw.get("tool_calls") or []:
        if not isinstance(item, Mapping):
            continue
        fn = item.get("function") if isinstance(item.get("function"), Mapping) else {}
        calls.append(
            ToolCall(
                id=str(item.get("id") or _new_id("call")),
                name=str(fn.get("name") or ""),
                arguments=str(fn.get("arguments") or "{}"),
            )
        )
    if role == "tool":
        return Turn(
            role="tool",
            content=_as_text(raw.get("content")),
            tool_call_id=str(raw.get("tool_call_id") or ""),
            name=str(raw.get("name") or ""),
        )
    return Turn(role=role, content=_as_text(raw.get("content")), tool_calls=calls)


def _response_format_from_chat(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    kind = str(raw.get("type") or "")
    if kind == "json_object":
        return {"type": "json_object"}
    if kind == "json_schema":
        block = raw.get("json_schema") if isinstance(raw.get("json_schema"), Mapping) else {}
        return {
            "type": "json_schema",
            "name": str(block.get("name") or "response"),
            "schema": dict(block.get("schema") or {}),
            "strict": bool(block.get("strict")),
        }
    return None


def from_chat(body: Mapping[str, Any]) -> Canonical:
    params = Params(
        model=str(body.get("model") or ""),
        max_tokens=_coerce_int(body.get("max_tokens")),
        temperature=_coerce_float(body.get("temperature")),
        top_p=_coerce_float(body.get("top_p")),
        stream=bool(body.get("stream")),
        stop=[str(item) for item in body.get("stop") or []] if isinstance(body.get("stop"), (list, tuple)) else [],
        tool_choice=body.get("tool_choice"),
        response_format=_response_format_from_chat(body.get("response_format")),
    )
    for raw in body.get("tools") or []:
        if isinstance(raw, Mapping):
            spec = _tool_spec_from_chat(raw)
            if spec is not None:
                params.tools.append(spec)
    turns = [
        _turn_from_chat_message(item)
        for item in body.get("messages") or []
        if isinstance(item, Mapping)
    ]
    return Canonical(params, turns)


def to_chat(canonical: Canonical) -> dict[str, Any]:
    params, turns = canonical.params, canonical.turns
    messages: list[dict[str, Any]] = []
    for turn in turns:
        if turn.role == "assistant" and turn.tool_calls:
            messages.append(
                {
                    "role": "assistant",
                    # Some relays require the key to be present even when null.
                    "content": turn.content or None,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {"name": call.name, "arguments": call.arguments or "{}"},
                        }
                        for call in turn.tool_calls
                    ],
                }
            )
        elif turn.role == "tool":
            entry: dict[str, Any] = {"role": "tool", "content": turn.content}
            if turn.tool_call_id:
                entry["tool_call_id"] = turn.tool_call_id
            if turn.name:
                entry["name"] = turn.name
            messages.append(entry)
        else:
            messages.append({"role": turn.role, "content": turn.content})
    body: dict[str, Any] = {"model": params.model, "messages": messages}
    if params.max_tokens is not None:
        body["max_tokens"] = params.max_tokens
    if params.temperature is not None:
        body["temperature"] = params.temperature
    if params.top_p is not None:
        body["top_p"] = params.top_p
    if params.stop:
        body["stop"] = params.stop
    if params.stream:
        body["stream"] = True
    if params.tools:
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters or {"type": "object", "properties": {}},
                },
            }
            for spec in params.tools
        ]
        if params.tool_choice is not None:
            body["tool_choice"] = params.tool_choice
    if params.response_format:
        if params.response_format["type"] == "json_object":
            body["response_format"] = {"type": "json_object"}
        else:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": params.response_format.get("name") or "response",
                    "strict": bool(params.response_format.get("strict")),
                    "schema": _strip_unsupported(params.response_format.get("schema") or {}),
                },
            }
    return body


# ── inbound: Responses ──────────────────────────────────────────────────────


def _tool_spec_from_responses(raw: Mapping[str, Any]) -> ToolSpec | None:
    if str(raw.get("type") or "function") != "function":
        return None
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    params = raw.get("parameters") if isinstance(raw.get("parameters"), Mapping) else {}
    return ToolSpec(name, str(raw.get("description") or ""), dict(params))


def _turn_from_responses_item(raw: Mapping[str, Any]) -> Turn | None:
    kind = str(raw.get("type") or "message")
    if kind == "function_call":
        return Turn(
            role="assistant",
            tool_calls=[
                ToolCall(
                    id=str(raw.get("call_id") or raw.get("id") or _new_id("call")),
                    name=str(raw.get("name") or ""),
                    arguments=str(raw.get("arguments") or "{}"),
                )
            ],
        )
    if kind == "function_call_output":
        return Turn(
            role="tool",
            content=_as_text(raw.get("output")),
            tool_call_id=str(raw.get("call_id") or ""),
        )
    if kind in {"reasoning", "item_reference"}:
        return None
    role = str(raw.get("role") or "user")
    if role == "developer":
        role = "system"
    return Turn(role=role, content=_as_text(raw.get("content")))


def from_responses(body: Mapping[str, Any]) -> Canonical:
    params = Params(
        model=str(body.get("model") or ""),
        max_tokens=_coerce_int(body.get("max_output_tokens")),
        temperature=_coerce_float(body.get("temperature")),
        top_p=_coerce_float(body.get("top_p")),
        stream=bool(body.get("stream")),
        tool_choice=body.get("tool_choice"),
    )
    for raw in body.get("tools") or []:
        if isinstance(raw, Mapping):
            spec = _tool_spec_from_responses(raw)
            if spec is not None:
                params.tools.append(spec)
    text_block = body.get("text") if isinstance(body.get("text"), Mapping) else {}
    fmt = text_block.get("format") if isinstance(text_block.get("format"), Mapping) else None
    if isinstance(fmt, Mapping):
        if str(fmt.get("type")) == "json_object":
            params.response_format = {"type": "json_object"}
        elif str(fmt.get("type")) == "json_schema":
            params.response_format = {
                "type": "json_schema",
                "name": str(fmt.get("name") or "response"),
                "schema": dict(fmt.get("schema") or {}),
                "strict": bool(fmt.get("strict")),
            }
    turns: list[Turn] = []
    instructions = body.get("instructions")
    if instructions:
        turns.append(Turn(role="system", content=_as_text(instructions)))
    payload = body.get("input")
    if isinstance(payload, str):
        turns.append(Turn(role="user", content=payload))
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            if isinstance(item, str):
                turns.append(Turn(role="user", content=item))
            elif isinstance(item, Mapping):
                turn = _turn_from_responses_item(item)
                if turn is not None:
                    turns.append(turn)
    return Canonical(params, turns)


def to_responses(canonical: Canonical) -> dict[str, Any]:
    params, turns = canonical.params, canonical.turns
    body: dict[str, Any] = {"model": params.model, "input": []}
    system = canonical.system_text()
    if system:
        body["instructions"] = system
    for turn in turns:
        if turn.role == "system":
            continue
        if turn.role == "assistant" and turn.tool_calls:
            if turn.content:
                body["input"].append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": turn.content}],
                    }
                )
            for call in turn.tool_calls:
                body["input"].append(
                    {
                        "type": "function_call",
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": call.arguments or "{}",
                    }
                )
        elif turn.role == "tool":
            body["input"].append(
                {
                    "type": "function_call_output",
                    "call_id": turn.tool_call_id,
                    "output": turn.content,
                }
            )
        else:
            part = "output_text" if turn.role == "assistant" else "input_text"
            body["input"].append(
                {
                    "type": "message",
                    "role": turn.role,
                    "content": [{"type": part, "text": turn.content}],
                }
            )
    if params.max_tokens is not None:
        body["max_output_tokens"] = params.max_tokens
    if params.temperature is not None:
        body["temperature"] = params.temperature
    if params.top_p is not None:
        body["top_p"] = params.top_p
    if params.stream:
        body["stream"] = True
    # ``store: false`` keeps the relay from persisting every worker turn.
    body["store"] = False
    if params.tools:
        body["tools"] = [
            {
                "type": "function",
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters or {"type": "object", "properties": {}},
                "strict": False,
            }
            for spec in params.tools
        ]
        if params.tool_choice is not None:
            body["tool_choice"] = params.tool_choice
    if params.response_format:
        if params.response_format["type"] == "json_object":
            body["text"] = {"format": {"type": "json_object"}}
        else:
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": params.response_format.get("name") or "response",
                    "strict": bool(params.response_format.get("strict")),
                    "schema": params.response_format.get("schema") or {},
                }
            }
    return body


# ── inbound: Anthropic Messages ─────────────────────────────────────────────


def _tool_spec_from_messages(raw: Mapping[str, Any]) -> ToolSpec | None:
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    schema = raw.get("input_schema") if isinstance(raw.get("input_schema"), Mapping) else {}
    return ToolSpec(name, str(raw.get("description") or ""), dict(schema))


def _turns_from_messages_item(raw: Mapping[str, Any]) -> list[Turn]:
    role = str(raw.get("role") or "user")
    content = raw.get("content")
    if isinstance(content, str):
        return [Turn(role=role, content=content)]
    prose: list[str] = []
    tool_uses: list[ToolCall] = []
    tool_results: list[Turn] = []
    for block in content or []:
        if not isinstance(block, Mapping):
            continue
        kind = str(block.get("type") or "text")
        if kind == "text":
            prose.append(_as_text(block.get("text")))
        elif kind == "tool_use":
            tool_uses.append(
                ToolCall(
                    id=str(block.get("id") or _new_id("call")),
                    name=str(block.get("name") or ""),
                    arguments=_json_dumps(block.get("input") or {}),
                )
            )
        elif kind == "tool_result":
            tool_results.append(
                Turn(
                    role="tool",
                    content=_as_text(block.get("content")),
                    tool_call_id=str(block.get("tool_use_id") or ""),
                )
            )
    turns: list[Turn] = []
    text = "".join(prose)
    if tool_uses:
        # Prose that accompanied the tool_use blocks belongs to the SAME
        # assistant message: that is exactly how the Chat wire represents it
        # (``content`` plus ``tool_calls`` on one message), and splitting it in
        # two would make the assistant appear to answer twice.
        turns.append(Turn(role="assistant", content=text, tool_calls=tool_uses))
    elif text:
        turns.append(Turn(role=role, content=text))
    turns.extend(tool_results)
    return turns


def from_messages(body: Mapping[str, Any]) -> Canonical:
    params = Params(
        model=str(body.get("model") or ""),
        max_tokens=_coerce_int(body.get("max_tokens")),
        temperature=_coerce_float(body.get("temperature")),
        top_p=_coerce_float(body.get("top_p")),
        stream=bool(body.get("stream")),
        stop=[str(item) for item in body.get("stop_sequences") or []]
        if isinstance(body.get("stop_sequences"), (list, tuple))
        else [],
        tool_choice=body.get("tool_choice"),
    )
    for raw in body.get("tools") or []:
        if isinstance(raw, Mapping):
            spec = _tool_spec_from_messages(raw)
            if spec is not None:
                params.tools.append(spec)
    turns: list[Turn] = []
    system = body.get("system")
    if system:
        turns.append(Turn(role="system", content=_as_text(system)))
    for item in body.get("messages") or []:
        if isinstance(item, Mapping):
            turns.extend(_turns_from_messages_item(item))
    return Canonical(params, turns)


def to_messages(canonical: Canonical) -> dict[str, Any]:
    params, turns = canonical.params, canonical.turns
    messages: list[dict[str, Any]] = []
    for turn in turns:
        if turn.role == "system":
            continue
        if turn.role == "assistant" and turn.tool_calls:
            blocks: list[dict[str, Any]] = []
            if turn.content:
                blocks.append({"type": "text", "text": turn.content})
            for call in turn.tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": call.parsed_arguments(),
                    }
                )
            messages.append({"role": "assistant", "content": blocks})
        elif turn.role == "tool":
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": turn.tool_call_id,
                            "content": turn.content,
                        }
                    ],
                }
            )
        else:
            messages.append({"role": turn.role, "content": turn.content})
    body: dict[str, Any] = {"model": params.model, "messages": messages}
    system = canonical.system_text()
    if system:
        body["system"] = system
    # ``max_tokens`` is required by the Messages API.
    body["max_tokens"] = params.max_tokens if params.max_tokens is not None else 4096
    if params.temperature is not None:
        body["temperature"] = params.temperature
    if params.top_p is not None:
        body["top_p"] = params.top_p
    if params.stop:
        body["stop_sequences"] = params.stop
    if params.stream:
        body["stream"] = True
    if params.tools:
        body["tools"] = [
            {
                "name": spec.name,
                "description": spec.description,
                "input_schema": spec.parameters or {"type": "object", "properties": {}},
            }
            for spec in params.tools
        ]
    return body


# ── dialect dispatch ────────────────────────────────────────────────────────

_DECODERS = {CHAT: from_chat, RESPONSES: from_responses, MESSAGES: from_messages}
_ENCODERS = {CHAT: to_chat, RESPONSES: to_responses, MESSAGES: to_messages}


def decode(dialect: str, body: Mapping[str, Any]) -> Canonical:
    decoder = _DECODERS.get(dialect)
    if decoder is None:
        raise ValueError(f"unsupported inbound dialect: {dialect!r}")
    return decoder(body)


def encode(dialect: str, canonical: Canonical) -> dict[str, Any]:
    encoder = _ENCODERS.get(dialect)
    if encoder is None:
        raise ValueError(f"unsupported outbound dialect: {dialect!r}")
    return encoder(canonical)


# ── result rendering: canonical answer -> each dialect's response object ────


@dataclass
class Answer:
    """What the upstream produced, independent of any dialect."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    response_id: str = ""


def _finish_for_chat(reason: str) -> str:
    return {
        "stop": "stop",
        "length": "length",
        "tool_calls": "tool_calls",
        "tool_use": "tool_calls",
        "end_turn": "stop",
        "max_tokens": "length",
        "function_call": "tool_calls",
    }.get(reason, "stop")


def chat_response(answer: Answer, *, created: int | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": answer.text or ""}
    if answer.tool_calls:
        message["content"] = answer.text or None
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments or "{}"},
            }
            for call in answer.tool_calls
        ]
    return {
        "id": answer.response_id or _new_id("chatcmpl"),
        "object": "chat.completion",
        "created": created if created is not None else int(time.time()),
        "model": answer.model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": _finish_for_chat(answer.finish_reason),
            }
        ],
        "usage": {
            "prompt_tokens": answer.input_tokens,
            "completion_tokens": answer.output_tokens,
            "total_tokens": answer.input_tokens + answer.output_tokens,
        },
    }


def _responses_output(answer: Answer) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    if answer.text or not answer.tool_calls:
        output.append(
            {
                "type": "message",
                "id": _new_id("msg"),
                "status": "completed",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": answer.text, "annotations": []}
                ],
            }
        )
    for call in answer.tool_calls:
        output.append(
            {
                "type": "function_call",
                "id": _new_id("fc"),
                "call_id": call.id,
                "name": call.name,
                "arguments": call.arguments or "{}",
                "status": "completed",
            }
        )
    return output


def responses_response(answer: Answer, *, created: int | None = None) -> dict[str, Any]:
    response_id = answer.response_id or _new_id("resp")
    return {
        "id": response_id,
        "object": "response",
        "created_at": created if created is not None else int(time.time()),
        "status": "completed",
        "model": answer.model,
        "output": _responses_output(answer),
        "output_text": answer.text,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": answer.input_tokens,
            "output_tokens": answer.output_tokens,
            "total_tokens": answer.input_tokens + answer.output_tokens,
        },
    }


def messages_response(answer: Answer, *, created: int | None = None) -> dict[str, Any]:
    del created
    blocks: list[dict[str, Any]] = []
    if answer.text or not answer.tool_calls:
        blocks.append({"type": "text", "text": answer.text})
    for call in answer.tool_calls:
        blocks.append(
            {
                "type": "tool_use",
                "id": call.id,
                "name": call.name,
                "input": call.parsed_arguments(),
            }
        )
    stop_reason = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "tool_use": "tool_use",
        "end_turn": "end_turn",
        "max_tokens": "max_tokens",
        "function_call": "tool_use",
    }.get(answer.finish_reason, "end_turn")
    return {
        "id": answer.response_id or _new_id("msg"),
        "type": "message",
        "role": "assistant",
        "model": answer.model,
        "content": blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": answer.input_tokens,
            "output_tokens": answer.output_tokens,
        },
    }


_RENDERERS = {
    CHAT: chat_response,
    RESPONSES: responses_response,
    MESSAGES: messages_response,
}


def render(dialect: str, answer: Answer, *, created: int | None = None) -> dict[str, Any]:
    renderer = _RENDERERS.get(dialect)
    if renderer is None:
        raise ValueError(f"unsupported outbound dialect: {dialect!r}")
    return renderer(answer, created=created)


# ── upstream response parsing ───────────────────────────────────────────────


def answer_from_chat(body: Mapping[str, Any]) -> Answer:
    choice = next(
        (item for item in body.get("choices") or [] if isinstance(item, Mapping)), {}
    )
    message = choice.get("message") if isinstance(choice.get("message"), Mapping) else {}
    calls: list[ToolCall] = []
    for item in message.get("tool_calls") or []:
        if not isinstance(item, Mapping):
            continue
        fn = item.get("function") if isinstance(item.get("function"), Mapping) else {}
        calls.append(
            ToolCall(
                id=str(item.get("id") or _new_id("call")),
                name=str(fn.get("name") or ""),
                arguments=str(fn.get("arguments") or "{}"),
            )
        )
    usage = body.get("usage") if isinstance(body.get("usage"), Mapping) else {}
    return Answer(
        text=_as_text(message.get("content")),
        tool_calls=calls,
        finish_reason=str(choice.get("finish_reason") or ("tool_calls" if calls else "stop")),
        model=str(body.get("model") or ""),
        input_tokens=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        response_id=str(body.get("id") or ""),
    )


def answer_from_responses(body: Mapping[str, Any]) -> Answer:
    text_parts: list[str] = []
    calls: list[ToolCall] = []
    for item in body.get("output") or []:
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("type") or "")
        if kind == "message":
            for block in item.get("content") or []:
                if isinstance(block, Mapping) and str(block.get("type")) in {
                    "output_text",
                    "text",
                }:
                    text_parts.append(_as_text(block.get("text")))
        elif kind == "function_call":
            calls.append(
                ToolCall(
                    id=str(item.get("call_id") or item.get("id") or _new_id("call")),
                    name=str(item.get("name") or ""),
                    arguments=str(item.get("arguments") or "{}"),
                )
            )
    if not text_parts and isinstance(body.get("output_text"), str):
        text_parts.append(body["output_text"])
    usage = body.get("usage") if isinstance(body.get("usage"), Mapping) else {}
    return Answer(
        text="".join(text_parts),
        tool_calls=calls,
        finish_reason="tool_calls" if calls else "stop",
        model=str(body.get("model") or ""),
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        response_id=str(body.get("id") or ""),
    )


def answer_from_messages(body: Mapping[str, Any]) -> Answer:
    text_parts: list[str] = []
    calls: list[ToolCall] = []
    for block in body.get("content") or []:
        if not isinstance(block, Mapping):
            continue
        kind = str(block.get("type") or "")
        if kind == "text":
            text_parts.append(_as_text(block.get("text")))
        elif kind == "tool_use":
            calls.append(
                ToolCall(
                    id=str(block.get("id") or _new_id("call")),
                    name=str(block.get("name") or ""),
                    arguments=_json_dumps(block.get("input") or {}),
                )
            )
    usage = body.get("usage") if isinstance(body.get("usage"), Mapping) else {}
    stop = str(body.get("stop_reason") or "")
    return Answer(
        text="".join(text_parts),
        tool_calls=calls,
        finish_reason=stop or ("tool_calls" if calls else "stop"),
        model=str(body.get("model") or ""),
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        response_id=str(body.get("id") or ""),
    )


_PARSERS = {
    CHAT: answer_from_chat,
    RESPONSES: answer_from_responses,
    MESSAGES: answer_from_messages,
}


def parse_answer(dialect: str, body: Mapping[str, Any]) -> Answer:
    parser = _PARSERS.get(dialect)
    if parser is None:
        raise ValueError(f"unsupported upstream dialect: {dialect!r}")
    return parser(body)


# ── endpoint URL resolution ─────────────────────────────────────────────────


#: Endpoint paths an operator may paste into base_url.  Stripped before the
#: target path is appended so a full path never doubles up.
_ENDPOINT_PATHS = (
    "/chat/completions",
    "/responses",
    "/completions",
    "/messages",
)


def resolve_endpoint(base_url: str, wire: str) -> str:
    """Build the absolute endpoint URL for ``wire`` from an operator base_url.

    Operators paste every shape imaginable, and blindly appending the path used
    to produce ``/v1/chat/completions/chat/completions``.  Accept the root, a
    ``/v1`` prefix, the full endpoint path, or even the *other* dialect's
    endpoint path (a switch service may legitimately be pointed at
    ``.../responses`` and be asked for chat).

    A bare host is left alone rather than upgraded to ``/v1``: ``api.deepseek.com``
    is served at ``/chat/completions``, so inserting ``/v1`` would break a
    configuration that works today.
    """

    root = str(base_url or "").strip().rstrip("/")
    if not root:
        raise ValueError("base_url is empty")
    for path in _ENDPOINT_PATHS:
        if root.endswith(path):
            root = root[: -len(path)]
            break
    suffix = "/chat/completions" if wire == CHAT else "/responses"
    return root + suffix


class SSEParser:
    """Incremental Server-Sent-Events decoder.

    Deliberately stateful: a network stream hands over arbitrary chunk
    boundaries, so a single ``event:``/``data:`` frame routinely arrives split
    across two chunks.  Splitting each chunk on newlines (the obvious
    implementation) silently drops every frame that straddles a boundary -- the
    exact failure mode that makes a translated stream hang mid-turn.

    Accepts both ``event:``-tagged frames and the bare ``data:`` frames some
    relays emit, and joins multi-line ``data:`` values with newlines per the
    SSE specification.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._event = ""
        self._data: list[str] = []

    def feed(self, text: str) -> Iterator[tuple[str, str]]:
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.rstrip("\r")
            if not line:
                if self._data:
                    yield self._event, "\n".join(self._data)
                self._event, self._data = "", []
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field == "event":
                self._event = value
            elif field == "data":
                self._data.append(value)

    def flush(self) -> Iterator[tuple[str, str]]:
        """Emit a trailing frame that arrived without its blank-line terminator."""

        if self._data:
            yield self._event, "\n".join(self._data)
        self._event, self._data = "", []


def iter_sse_blocks(chunks: Iterable[str]) -> Iterable[tuple[str, str]]:
    """Decode a complete SSE stream held in memory into ``(event, data)`` pairs."""

    parser = SSEParser()
    for chunk in chunks:
        yield from parser.feed(chunk)
    yield from parser.flush()


async def aiter_sse_blocks(
    chunks: AsyncIterable[str],
) -> AsyncIterator[tuple[str, str]]:
    """Async twin of :func:`iter_sse_blocks` for a live network stream."""

    parser = SSEParser()
    async for chunk in chunks:
        for frame in parser.feed(chunk):
            yield frame
    for frame in parser.flush():
        yield frame
