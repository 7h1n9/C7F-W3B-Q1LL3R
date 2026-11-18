"""Wire-protocol helpers for OpenAI-style model providers (shared core).

This module lives in the vendored ``muteki`` core because BOTH sides need it
and the vendored package never imports ``app.*``: ``app.engines.wire``
re-exports these names for the backend, and ``muteki.core.llm`` uses them for
the container-side Worker.  Keeping one implementation is what stops the two
translation paths from drifting apart.  Pure stdlib on purpose -- it is
imported from a settings schema, so it must not pull httpx or pydantic.


Two problems this module solves, both of which used to fail silently or with an
unhelpful 404:

**Endpoint resolution.** ``base_url`` is operator input, and operators paste the
root (``https://relay.example/v1``), the bare host (``https://api.deepseek.com``)
or the full endpoint path (``https://relay.example/v1/chat/completions``).
Blindly appending the path produced ``/v1/chat/completions/chat/completions``.
:func:`resolve_endpoint` strips a recognised endpoint path before appending the
target one, and never inserts a ``/v1`` that was not there -- ``api.deepseek.com``
is served at ``/chat/completions``, so adding a segment would break a
configuration that works today.

**Responses support.** Some providers only speak the OpenAI *Responses* wire
(``/responses``) while the rest of this codebase -- the Reason model, the
structured-action engine, the model test page -- is written against Chat
Completions.  Rather than teach every caller a second protocol, the two
translators below convert at the HTTP boundary so the engine keeps parsing one
shape.  They are intentionally lossless in the direction that matters: an
assistant tool call survives the round trip with its id intact, because the
caller has to match it to the tool result it sends back next turn.
"""

from __future__ import annotations

from typing import Any, Mapping

CHAT_COMPLETIONS = "chat_completions"
RESPONSES = "responses"
WIRE_API_VALUES = (CHAT_COMPLETIONS, RESPONSES)

#: Endpoint paths an operator may paste into ``base_url``.
_ENDPOINT_PATHS = ("/chat/completions", "/responses", "/completions", "/messages")


def normalize_wire_api(value: Any, *, default: str = CHAT_COMPLETIONS) -> str:
    """Coerce a stored ``wire_api`` into a supported value."""

    text = str(value or "").strip().lower()
    if text in {"responses", "response"}:
        return RESPONSES
    if text in {"chat_completions", "chat", "completions"}:
        return CHAT_COMPLETIONS
    return default


def strip_endpoint_path(base_url: str) -> str:
    """Drop a pasted endpoint suffix, leaving the API root.

    One definition of "what counts as a pasted endpoint" for every direction
    that builds a URL from a stored ``base_url``.  The Anthropic-Messages probe
    needs the same tolerance this module already gives the OpenAI wire: a relay
    that serves both ``/v1/messages`` and ``/v1/chat/completions`` is common, so
    an operator who pasted either one must not get
    ``.../v1/chat/completions/v1/messages`` -- a path no relay serves, which
    reads as a broken config instead of a double suffix.
    """

    root = str(base_url or "").strip().rstrip("/")
    for path in _ENDPOINT_PATHS:
        if root.endswith(path):
            root = root[: -len(path)].rstrip("/")
            break
    return root


def resolve_endpoint(base_url: str, wire_api: str = CHAT_COMPLETIONS) -> str:
    """Absolute endpoint URL for ``wire_api``, tolerating any ``base_url`` shape."""

    root = strip_endpoint_path(base_url)
    if not root:
        raise ValueError("base_url is empty")
    suffix = "/responses" if normalize_wire_api(wire_api) == RESPONSES else "/chat/completions"
    return root + suffix


def _text_of(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("text", "content", "output"):
            if key in value:
                return _text_of(value[key])
        return ""
    if isinstance(value, (list, tuple)):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping):
                text = _text_of(item)
                if text:
                    parts.append(text)
        return "".join(parts)
    return str(value)


def to_responses_request(chat_payload: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a Chat Completions request body into a Responses request body.

    ``stream`` is deliberately dropped: every caller of this translator reads the
    whole JSON body, so asking the upstream to stream would only make the read
    fail.  The model test page probes ``/responses`` with its own streaming
    client when it needs to.
    """

    instructions: list[str] = []
    items: list[dict[str, Any]] = []
    for message in chat_payload.get("messages") or []:
        if not isinstance(message, Mapping):
            continue
        role = str(message.get("role") or "user")
        content = _text_of(message.get("content"))
        if role in {"system", "developer"}:
            if content:
                instructions.append(content)
            continue
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": str(message.get("tool_call_id") or ""),
                    "output": content,
                }
            )
            continue
        calls = message.get("tool_calls")
        if role == "assistant" and isinstance(calls, (list, tuple)) and calls:
            if content:
                items.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": content}],
                    }
                )
            for call in calls:
                if not isinstance(call, Mapping):
                    continue
                function = call.get("function") if isinstance(call.get("function"), Mapping) else {}
                items.append(
                    {
                        "type": "function_call",
                        "call_id": str(call.get("id") or ""),
                        "name": str(function.get("name") or ""),
                        "arguments": str(function.get("arguments") or "{}"),
                    }
                )
            continue
        part = "output_text" if role == "assistant" else "input_text"
        items.append(
            {
                "type": "message",
                "role": role,
                "content": [{"type": part, "text": content}],
            }
        )

    body: dict[str, Any] = {"input": items}
    if instructions:
        body["instructions"] = "\n\n".join(instructions)
    if chat_payload.get("model"):
        body["model"] = chat_payload["model"]
    if chat_payload.get("max_tokens") is not None:
        body["max_output_tokens"] = chat_payload["max_tokens"]
    if chat_payload.get("temperature") is not None:
        body["temperature"] = chat_payload["temperature"]
    if chat_payload.get("top_p") is not None:
        body["top_p"] = chat_payload["top_p"]
    # Keep a relay from persisting every worker turn.
    body["store"] = False

    tools = []
    for tool in chat_payload.get("tools") or []:
        if not isinstance(tool, Mapping):
            continue
        function = tool.get("function") if isinstance(tool.get("function"), Mapping) else tool
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        tools.append(
            {
                "type": "function",
                "name": name,
                "description": str(function.get("description") or ""),
                "parameters": function.get("parameters")
                if isinstance(function.get("parameters"), Mapping)
                else {"type": "object", "properties": {}},
                "strict": False,
            }
        )
    if tools:
        body["tools"] = tools
        if chat_payload.get("tool_choice") is not None:
            body["tool_choice"] = chat_payload["tool_choice"]

    response_format = chat_payload.get("response_format")
    if isinstance(response_format, Mapping):
        kind = str(response_format.get("type") or "")
        if kind == "json_object":
            body["text"] = {"format": {"type": "json_object"}}
        elif kind == "json_schema":
            block = (
                response_format.get("json_schema")
                if isinstance(response_format.get("json_schema"), Mapping)
                else {}
            )
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": str(block.get("name") or "response"),
                    "strict": bool(block.get("strict")),
                    "schema": block.get("schema") if isinstance(block.get("schema"), Mapping) else {},
                }
            }
    return body


def responses_to_chat_body(body: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a Responses response body into the Chat Completions shape.

    The engine downstream only knows ``choices[0].message`` and
    ``usage.prompt_tokens``; presenting the Responses payload in that shape keeps
    every parser, retry branch and trace field working unchanged.
    """

    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
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
                    text_parts.append(_text_of(block.get("text")))
        elif kind == "function_call":
            tool_calls.append(
                {
                    "id": str(item.get("call_id") or item.get("id") or ""),
                    "type": "function",
                    "function": {
                        "name": str(item.get("name") or ""),
                        "arguments": str(item.get("arguments") or "{}"),
                    },
                }
            )
    if not text_parts and isinstance(body.get("output_text"), str):
        text_parts.append(body["output_text"])

    usage = body.get("usage") if isinstance(body.get("usage"), Mapping) else {}
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text_parts) if text_parts else None,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": str(body.get("id") or ""),
        "object": "chat.completion",
        "model": str(body.get("model") or ""),
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": {
            "prompt_tokens": int(usage.get("input_tokens") or 0),
            "completion_tokens": int(usage.get("output_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0)
            or (int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)),
        },
        # Provenance for the trace, so a Responses-backed turn is not mistaken
        # for a Chat one when reading a run afterwards.
        "_wire_api": RESPONSES,
    }


__all__ = [
    "CHAT_COMPLETIONS",
    "RESPONSES",
    "WIRE_API_VALUES",
    "normalize_wire_api",
    "resolve_endpoint",
    "responses_to_chat_body",
    "strip_endpoint_path",
    "to_responses_request",
]
