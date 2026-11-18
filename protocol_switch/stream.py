"""Streaming translation: one canonical delta stream, three SSE encodings.

Every upstream dialect is decoded into the same :class:`Chunk` sequence, and
every downstream dialect is produced by feeding that sequence to an encoder.
That keeps the streaming matrix (3 inbound x 2 upstream x 3 downstream) linear
instead of quadratic.

Only ``transform`` and the encoders do work here -- the transport lives in
``server.py`` -- so the whole streaming path is unit-testable by feeding it
literal SSE text.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterable, AsyncIterator, Iterable, Mapping

from .translate import (
    CHAT,
    MESSAGES,
    RESPONSES,
    Answer,
    ToolCall,
    _new_id,
    aiter_sse_blocks,
    iter_sse_blocks,
)

__all__ = [
    "Chunk",
    "atransform",
    "encode_downstream",
    "iter_upstream_chunks",
    "aiter_upstream_chunks",
    "transform",
]


@dataclass
class Chunk:
    """One canonical incremental event."""

    text: str = ""
    #: Index of the tool call this fragment belongs to (None for prose).
    tool_index: int | None = None
    tool_id: str = ""
    tool_name: str = ""
    tool_arguments: str = ""
    finish_reason: str = ""
    usage: dict[str, Any] | None = None
    model: str = ""
    response_id: str = ""


def _json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _frame(event: str, payload: Mapping[str, Any]) -> str:
    """Render one SSE frame.  ``event:`` is omitted when the caller passes ""."""

    head = f"event: {event}\n" if event else ""
    return f"{head}data: {_json(payload)}\n\n"


# ── upstream decoders ───────────────────────────────────────────────────────


def _decode_chat(event: str, data: str) -> Chunk | None:
    del event
    if data.strip() == "[DONE]":
        return Chunk(finish_reason="stop")
    try:
        payload = json.loads(data)
    except ValueError:
        return None
    if not isinstance(payload, Mapping):
        return None
    usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else None
    choice = next(
        (item for item in payload.get("choices") or [] if isinstance(item, Mapping)), None
    )
    if choice is None:
        return Chunk(usage=dict(usage) if usage else None, model=str(payload.get("model") or ""))
    delta = choice.get("delta") if isinstance(choice.get("delta"), Mapping) else {}
    text = delta.get("content")
    text = text if isinstance(text, str) else ""
    chunk = Chunk(
        text=text,
        finish_reason=str(choice.get("finish_reason") or ""),
        usage=dict(usage) if usage else None,
        model=str(payload.get("model") or ""),
        response_id=str(payload.get("id") or ""),
    )
    for item in delta.get("tool_calls") or []:
        if not isinstance(item, Mapping):
            continue
        fn = item.get("function") if isinstance(item.get("function"), Mapping) else {}
        chunk.tool_index = int(item.get("index") or 0)
        chunk.tool_id = str(item.get("id") or "")
        chunk.tool_name = str(fn.get("name") or "")
        args = fn.get("arguments")
        chunk.tool_arguments = args if isinstance(args, str) else ""
        break
    return chunk


def _decode_responses(event: str, data: str) -> Chunk | None:
    try:
        payload = json.loads(data)
    except ValueError:
        return None
    if not isinstance(payload, Mapping):
        return None
    kind = str(payload.get("type") or event or "")
    if kind == "response.output_text.delta":
        return Chunk(text=str(payload.get("delta") or ""))
    if kind == "response.function_call_arguments.delta":
        return Chunk(
            tool_index=int(payload.get("output_index") or 0),
            tool_id=str(payload.get("item_id") or ""),
            tool_arguments=str(payload.get("delta") or ""),
        )
    if kind == "response.output_item.added":
        item = payload.get("item") if isinstance(payload.get("item"), Mapping) else {}
        if str(item.get("type") or "") == "function_call":
            return Chunk(
                tool_index=int(payload.get("output_index") or 0),
                tool_id=str(item.get("call_id") or item.get("id") or ""),
                tool_name=str(item.get("name") or ""),
                tool_arguments=str(item.get("arguments") or ""),
            )
        return None
    if kind == "response.completed":
        response = payload.get("response") if isinstance(payload.get("response"), Mapping) else {}
        usage = response.get("usage") if isinstance(response.get("usage"), Mapping) else None
        return Chunk(
            finish_reason="stop",
            usage=dict(usage) if usage else None,
            model=str(response.get("model") or ""),
            response_id=str(response.get("id") or ""),
        )
    if kind in {"response.failed", "response.incomplete", "error"}:
        return Chunk(finish_reason="stop")
    return None


def _decode_messages(event: str, data: str) -> Chunk | None:
    try:
        payload = json.loads(data)
    except ValueError:
        return None
    if not isinstance(payload, Mapping):
        return None
    kind = str(payload.get("type") or event or "")
    if kind == "message_start":
        message = payload.get("message") if isinstance(payload.get("message"), Mapping) else {}
        usage = message.get("usage") if isinstance(message.get("usage"), Mapping) else None
        return Chunk(
            usage=dict(usage) if usage else None,
            model=str(message.get("model") or ""),
            response_id=str(message.get("id") or ""),
        )
    if kind == "content_block_start":
        block = payload.get("content_block") if isinstance(payload.get("content_block"), Mapping) else {}
        if str(block.get("type") or "") == "tool_use":
            return Chunk(
                tool_index=int(payload.get("index") or 0),
                tool_id=str(block.get("id") or ""),
                tool_name=str(block.get("name") or ""),
            )
        text = block.get("text")
        return Chunk(text=text if isinstance(text, str) else "")
    if kind == "content_block_delta":
        delta = payload.get("delta") if isinstance(payload.get("delta"), Mapping) else {}
        dtype = str(delta.get("type") or "")
        if dtype == "text_delta":
            return Chunk(text=str(delta.get("text") or ""))
        if dtype == "input_json_delta":
            return Chunk(
                tool_index=int(payload.get("index") or 0),
                tool_arguments=str(delta.get("partial_json") or ""),
            )
        return None
    if kind == "message_delta":
        delta = payload.get("delta") if isinstance(payload.get("delta"), Mapping) else {}
        usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else None
        return Chunk(
            finish_reason=str(delta.get("stop_reason") or ""),
            usage=dict(usage) if usage else None,
        )
    if kind == "message_stop":
        return Chunk(finish_reason="stop")
    return None


_DECODERS = {
    CHAT: _decode_chat,
    RESPONSES: _decode_responses,
    MESSAGES: _decode_messages,
}


def _decoder_for(dialect: str):
    decoder = _DECODERS.get(dialect)
    if decoder is None:
        raise ValueError(f"unsupported upstream dialect: {dialect!r}")
    return decoder


def iter_upstream_chunks(dialect: str, chunks: Iterable[str]) -> Iterable[Chunk]:
    """Decode a complete upstream SSE stream into canonical chunks."""

    decoder = _decoder_for(dialect)
    for event, data in iter_sse_blocks(chunks):
        chunk = decoder(event, data)
        if chunk is not None:
            yield chunk


async def aiter_upstream_chunks(
    dialect: str, chunks: AsyncIterable[str]
) -> AsyncIterator[Chunk]:
    """Async twin of :func:`iter_upstream_chunks` for a live upstream stream."""

    decoder = _decoder_for(dialect)
    async for event, data in aiter_sse_blocks(chunks):
        chunk = decoder(event, data)
        if chunk is not None:
            yield chunk


# ── downstream encoders ─────────────────────────────────────────────────────


class _ChatEncoder:
    def __init__(self, model: str, created: int | None = None) -> None:
        self.model = model
        self.created = created if created is not None else int(time.time())
        self.id = _new_id("chatcmpl")
        self._role_sent = False

    def _envelope(self, delta: Mapping[str, Any], finish: str | None) -> str:
        return _frame(
            "",
            {
                "id": self.id,
                "object": "chat.completion.chunk",
                "created": self.created,
                "model": self.model,
                "choices": [{"index": 0, "delta": dict(delta), "finish_reason": finish}],
            },
        )

    def feed(self, chunk: Chunk) -> list[str]:
        out: list[str] = []
        if not self._role_sent:
            self._role_sent = True
            out.append(self._envelope({"role": "assistant", "content": ""}, None))
        if chunk.text:
            out.append(self._envelope({"content": chunk.text}, None))
        if chunk.tool_index is not None or chunk.tool_arguments:
            entry: dict[str, Any] = {"index": chunk.tool_index or 0}
            if chunk.tool_id:
                entry["id"] = chunk.tool_id
                entry["type"] = "function"
            fn: dict[str, Any] = {}
            if chunk.tool_name:
                fn["name"] = chunk.tool_name
            if chunk.tool_arguments:
                fn["arguments"] = chunk.tool_arguments
            if fn:
                entry["function"] = fn
            out.append(self._envelope({"tool_calls": [entry]}, None))
        return out

    def finish(self, answer: Answer | None = None) -> list[str]:
        reason = answer.finish_reason if answer else "stop"
        mapped = "tool_calls" if answer and answer.tool_calls else (
            "length" if reason in {"length", "max_tokens"} else "stop"
        )
        out = [self._envelope({}, mapped)]
        if answer is not None:
            out.append(
                _frame(
                    "",
                    {
                        "id": self.id,
                        "object": "chat.completion.chunk",
                        "created": self.created,
                        "model": self.model,
                        "choices": [],
                        "usage": {
                            "prompt_tokens": answer.input_tokens,
                            "completion_tokens": answer.output_tokens,
                            "total_tokens": answer.input_tokens + answer.output_tokens,
                        },
                    },
                )
            )
        out.append("data: [DONE]\n\n")
        return out


class _ResponsesEncoder:
    """Emit the Responses event sequence Codex consumes."""

    def __init__(self, model: str, created: int | None = None) -> None:
        self.model = model
        self.created = created if created is not None else int(time.time())
        self.id = _new_id("resp")
        self._text_item_id = _new_id("msg")
        self._text_open = False
        self._text_buffer: list[str] = []
        self._tools: dict[int, dict[str, Any]] = {}
        self._order: list[tuple[str, int]] = []

    def _shell(self, status: str = "in_progress") -> dict[str, Any]:
        return {
            "id": self.id,
            "object": "response",
            "created_at": self.created,
            "status": status,
            "model": self.model,
            "output": [],
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "usage": None,
        }

    def start(self) -> list[str]:
        return [
            _frame("response.created", {"type": "response.created", "response": self._shell()}),
            _frame(
                "response.in_progress",
                {"type": "response.in_progress", "response": self._shell()},
            ),
        ]

    def feed(self, chunk: Chunk) -> list[str]:
        out: list[str] = []
        if chunk.text:
            if not self._text_open:
                self._text_open = True
                self._order.append(("message", 0))
                out.append(
                    _frame(
                        "response.output_item.added",
                        {
                            "type": "response.output_item.added",
                            "output_index": 0,
                            "item": {
                                "id": self._text_item_id,
                                "type": "message",
                                "status": "in_progress",
                                "role": "assistant",
                                "content": [],
                            },
                        },
                    )
                )
                out.append(
                    _frame(
                        "response.content_part.added",
                        {
                            "type": "response.content_part.added",
                            "item_id": self._text_item_id,
                            "output_index": 0,
                            "content_index": 0,
                            "part": {"type": "output_text", "text": "", "annotations": []},
                        },
                    )
                )
            self._text_buffer.append(chunk.text)
            out.append(
                _frame(
                    "response.output_text.delta",
                    {
                        "type": "response.output_text.delta",
                        "item_id": self._text_item_id,
                        "output_index": 0,
                        "content_index": 0,
                        "delta": chunk.text,
                    },
                )
            )
        if chunk.tool_index is not None or chunk.tool_arguments or chunk.tool_name:
            index = chunk.tool_index or 0
            slot = self._tools.get(index)
            if slot is None:
                slot = {
                    "item_id": _new_id("fc"),
                    "call_id": chunk.tool_id or _new_id("call"),
                    "name": chunk.tool_name,
                    "arguments": [],
                    "output_index": len(self._order) + 1,
                }
                self._tools[index] = slot
                self._order.append(("function_call", slot["output_index"]))
                out.append(
                    _frame(
                        "response.output_item.added",
                        {
                            "type": "response.output_item.added",
                            "output_index": slot["output_index"],
                            "item": {
                                "id": slot["item_id"],
                                "type": "function_call",
                                "status": "in_progress",
                                "call_id": slot["call_id"],
                                "name": slot["name"],
                                "arguments": "",
                            },
                        },
                    )
                )
            else:
                if chunk.tool_id:
                    slot["call_id"] = chunk.tool_id
                if chunk.tool_name:
                    slot["name"] = chunk.tool_name
            if chunk.tool_arguments:
                slot["arguments"].append(chunk.tool_arguments)
                out.append(
                    _frame(
                        "response.function_call_arguments.delta",
                        {
                            "type": "response.function_call_arguments.delta",
                            "item_id": slot["item_id"],
                            "output_index": slot["output_index"],
                            "delta": chunk.tool_arguments,
                        },
                    )
                )
        return out

    def finish(self, answer: Answer | None = None) -> list[str]:
        out: list[str] = []
        text = "".join(self._text_buffer)
        if self._text_open:
            out.append(
                _frame(
                    "response.output_text.done",
                    {
                        "type": "response.output_text.done",
                        "item_id": self._text_item_id,
                        "output_index": 0,
                        "content_index": 0,
                        "text": text,
                    },
                )
            )
            out.append(
                _frame(
                    "response.content_part.done",
                    {
                        "type": "response.content_part.done",
                        "item_id": self._text_item_id,
                        "output_index": 0,
                        "content_index": 0,
                        "part": {
                            "type": "output_text",
                            "text": text,
                            "annotations": [],
                        },
                    },
                )
            )
            out.append(
                _frame(
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": {
                            "id": self._text_item_id,
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": text, "annotations": []}
                            ],
                        },
                    },
                )
            )
        for slot in self._tools.values():
            arguments = "".join(slot["arguments"]) or "{}"
            out.append(
                _frame(
                    "response.function_call_arguments.done",
                    {
                        "type": "response.function_call_arguments.done",
                        "item_id": slot["item_id"],
                        "output_index": slot["output_index"],
                        "arguments": arguments,
                    },
                )
            )
            out.append(
                _frame(
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "output_index": slot["output_index"],
                        "item": {
                            "id": slot["item_id"],
                            "type": "function_call",
                            "status": "completed",
                            "call_id": slot["call_id"],
                            "name": slot["name"],
                            "arguments": arguments,
                        },
                    },
                )
            )
        final = self._shell("completed")
        final["output"] = []
        if self._text_open:
            final["output"].append(
                {
                    "id": self._text_item_id,
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": text, "annotations": []}
                    ],
                }
            )
        for slot in self._tools.values():
            final["output"].append(
                {
                    "id": slot["item_id"],
                    "type": "function_call",
                    "status": "completed",
                    "call_id": slot["call_id"],
                    "name": slot["name"],
                    "arguments": "".join(slot["arguments"]) or "{}",
                }
            )
        final["output_text"] = text
        final["usage"] = {
            "input_tokens": answer.input_tokens if answer else 0,
            "output_tokens": answer.output_tokens if answer else 0,
            "total_tokens": (answer.input_tokens + answer.output_tokens) if answer else 0,
        }
        out.append(_frame("response.completed", {"type": "response.completed", "response": final}))
        return out


class _MessagesEncoder:
    """Emit the Anthropic Messages event sequence the Claude CLI consumes."""

    def __init__(self, model: str, created: int | None = None) -> None:
        del created
        self.model = model
        self.id = _new_id("msg")
        self._text_index: int | None = None
        self._next_index = 0
        self._tools: dict[int, dict[str, Any]] = {}
        self._input_tokens = 0
        self._output_tokens = 0

    def start(self) -> list[str]:
        return [
            _frame(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": self.id,
                        "type": "message",
                        "role": "assistant",
                        "model": self.model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    },
                },
            )
        ]

    def feed(self, chunk: Chunk) -> list[str]:
        out: list[str] = []
        if chunk.usage:
            self._input_tokens = int(
                chunk.usage.get("input_tokens") or self._input_tokens
            )
            self._output_tokens = int(
                chunk.usage.get("output_tokens") or self._output_tokens
            )
        if chunk.text:
            if self._text_index is None:
                self._text_index = self._next_index
                self._next_index += 1
                out.append(
                    _frame(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": self._text_index,
                            "content_block": {"type": "text", "text": ""},
                        },
                    )
                )
            out.append(
                _frame(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": self._text_index,
                        "delta": {"type": "text_delta", "text": chunk.text},
                    },
                )
            )
        if chunk.tool_index is not None or chunk.tool_name or chunk.tool_arguments:
            key = chunk.tool_index or 0
            slot = self._tools.get(key)
            if slot is None:
                slot = {
                    "index": self._next_index,
                    "id": chunk.tool_id or _new_id("toolu"),
                    "name": chunk.tool_name,
                }
                self._next_index += 1
                self._tools[key] = slot
                out.append(
                    _frame(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": slot["index"],
                            "content_block": {
                                "type": "tool_use",
                                "id": slot["id"],
                                "name": slot["name"],
                                "input": {},
                            },
                        },
                    )
                )
            else:
                if chunk.tool_id:
                    slot["id"] = chunk.tool_id
                if chunk.tool_name:
                    slot["name"] = chunk.tool_name
            if chunk.tool_arguments:
                out.append(
                    _frame(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": slot["index"],
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": chunk.tool_arguments,
                            },
                        },
                    )
                )
        return out

    def finish(self, answer: Answer | None = None) -> list[str]:
        out: list[str] = []
        if self._text_index is not None:
            out.append(
                _frame(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": self._text_index},
                )
            )
        for slot in self._tools.values():
            out.append(
                _frame(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": slot["index"]},
                )
            )
        if answer is not None and answer.tool_calls:
            stop_reason = "tool_use"
        elif answer is not None and answer.finish_reason in {"length", "max_tokens"}:
            stop_reason = "max_tokens"
        else:
            stop_reason = "end_turn"
        out.append(
            _frame(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {
                        "input_tokens": answer.input_tokens if answer else self._input_tokens,
                        "output_tokens": answer.output_tokens if answer else self._output_tokens,
                    },
                },
            )
        )
        out.append(_frame("message_stop", {"type": "message_stop"}))
        return out


_ENCODERS = {
    CHAT: _ChatEncoder,
    RESPONSES: _ResponsesEncoder,
    MESSAGES: _MessagesEncoder,
}


def encode_downstream(
    dialect: str, model: str, *, created: int | None = None
) -> Any:
    encoder = _ENCODERS.get(dialect)
    if encoder is None:
        raise ValueError(f"unsupported outbound dialect: {dialect!r}")
    return encoder(model, created)


class _StreamState:
    """Accumulates upstream deltas while an encoder emits downstream frames.

    Shared by the sync and async drivers so the two cannot drift: the terminal
    event must carry the same complete answer in both cases.
    """

    def __init__(self, encoder: Any, model: str) -> None:
        self.encoder = encoder
        self.model = model
        self.text_parts: list[str] = []
        self.toolcalls: dict[int, ToolCall] = {}
        self.usage: dict[str, Any] = {}
        # Named ``finish_reason`` on purpose: ``finish`` is the terminal-frame
        # method below, and assigning to it would shadow the method with a str.
        self.finish_reason = ""

    def start(self) -> list[str]:
        starter = getattr(self.encoder, "start", None)
        return list(starter()) if callable(starter) else []

    def feed(self, chunk: Chunk) -> list[str]:
        if chunk.model:
            self.model = chunk.model
        if chunk.text:
            self.text_parts.append(chunk.text)
        if chunk.tool_index is not None or chunk.tool_arguments or chunk.tool_name:
            key = chunk.tool_index or 0
            call = self.toolcalls.get(key)
            if call is None:
                call = ToolCall(
                    id=chunk.tool_id or _new_id("call"), name=chunk.tool_name, arguments=""
                )
                self.toolcalls[key] = call
            else:
                if chunk.tool_id:
                    call.id = chunk.tool_id
                if chunk.tool_name:
                    call.name = chunk.tool_name
            if chunk.tool_arguments:
                call.arguments += chunk.tool_arguments
        if chunk.usage:
            self.usage.update(chunk.usage)
        if chunk.finish_reason:
            self.finish_reason = chunk.finish_reason
        return self.encoder.feed(chunk)

    def answer(self) -> Answer:
        calls = [self.toolcalls[key] for key in sorted(self.toolcalls)]
        return Answer(
            text="".join(self.text_parts),
            tool_calls=calls,
            finish_reason=self.finish_reason or ("tool_calls" if calls else "stop"),
            model=self.model,
            input_tokens=int(self.usage.get("input_tokens") or self.usage.get("prompt_tokens") or 0),
            output_tokens=int(
                self.usage.get("output_tokens") or self.usage.get("completion_tokens") or 0
            ),
        )

    def finish(self, answer: Answer | None) -> list[str]:
        return self.encoder.finish(answer if answer is not None else self.answer())


def transform(
    *,
    upstream: str,
    downstream: str,
    chunks: Iterable[str],
    model: str,
    answer: Answer | None = None,
) -> Iterable[str]:
    """Translate a complete upstream SSE stream into downstream SSE frames.

    ``chunks`` yields raw upstream text; the return value yields ready-to-send
    downstream frames.  See :func:`atransform` for the live-network twin.
    """

    state = _StreamState(encode_downstream(downstream, model), model)
    for frame in state.start():
        yield frame
    for chunk in iter_upstream_chunks(upstream, chunks):
        for frame in state.feed(chunk):
            yield frame
    for frame in state.finish(answer):
        yield frame


async def atransform(
    *,
    upstream: str,
    downstream: str,
    chunks: AsyncIterable[str],
    model: str,
    answer: Answer | None = None,
) -> AsyncIterator[str]:
    """Async twin of :func:`transform` for a live upstream connection."""

    state = _StreamState(encode_downstream(downstream, model), model)
    for frame in state.start():
        yield frame
    async for chunk in aiter_upstream_chunks(upstream, chunks):
        for frame in state.feed(chunk):
            yield frame
    for frame in state.finish(answer):
        yield frame
