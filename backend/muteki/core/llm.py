"""DeepSeek (OpenAI-compatible) LLM client with reasoning-model handling.

Empirically verified against the temporary endpoint:
- Models are REASONING models: responses split `reasoning_content` from
  `content`, and tokens go to reasoning FIRST. `max_tokens` must be generous or
  `content` comes back empty with finish_reason=length. We default high.
- Streaming deltas carry `reasoning_content` and `content` as separate fields ->
  emitted as REASONING_DELTA vs TEXT_MESSAGE_DELTA.
- Tool calling works (finish_reason=tool_calls, valid JSON args). In streaming
  mode tool_calls arrive fragmented across deltas (index + partial arguments);
  we reassemble them.

Usage from every call is fed to the CostController.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

import httpx

from muteki.core.cost import CostController
from muteki.core.event_bus import EventBus
from muteki.core.events import Event, EventType
from muteki.core.wire import (
    CHAT_COMPLETIONS as _CHAT_COMPLETIONS,
    RESPONSES as _RESPONSES,
    normalize_wire_api as _normalize_wire_api,
    resolve_endpoint as _resolve_endpoint,
    responses_to_chat_body as _responses_to_chat_body,
    to_responses_request as _to_responses_request,
)

DEFAULT_BASE_URL = os.environ.get("MUTEKI_DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _raise_for_status(r: httpx.Response) -> None:
    """Like httpx.raise_for_status but includes the response body — the API's
    error message (e.g. which field is malformed) is otherwise swallowed."""
    if r.status_code < 400:
        return
    body = r.text[:2000]
    raise httpx.HTTPStatusError(
        f"{r.status_code} from {r.request.url}: {body}",
        request=r.request,
        response=r,
    )


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string as returned by the model

    def parsed_args(self) -> dict[str, Any]:
        try:
            return json.loads(self.arguments) if self.arguments else {}
        except json.JSONDecodeError:
            return {}


@dataclass
class LLMResponse:
    content: str
    reasoning: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


@dataclass
class ModelSpec:
    """A configured model 'persona' in the swarm lineup."""

    solver_id: str
    model: str
    temperature: float = 0.4
    max_tokens: int = 8000
    label: str = ""
    role: str = ""  # strategic-prior key; "" = generalist (no preamble)


class LLMClient:
    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        bus: Optional[EventBus] = None,
        cost: Optional[CostController] = None,
        timeout: float = 180.0,
        overall_timeout: float = 300.0,
        trust_env: Optional[bool] = None,
        wire_api: str = _CHAT_COMPLETIONS,
    ) -> None:
        self.api_key = api_key or os.environ.get("MUTEKI_DEEPSEEK_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        # ``responses`` is for a provider that only exposes the Responses API.
        # The two readers below fold its event sequence back into the Chat chunk
        # shape, so no caller needs a second parser.
        self.wire_api = _normalize_wire_api(wire_api)
        self.bus = bus
        self.cost = cost
        # Keep tests and local evals isolated from desktop-wide proxy settings.
        # Users who really need a proxy for the LLM API can opt in explicitly.
        self.trust_env = _env_bool("MUTEKI_LLM_TRUST_ENV", False) if trust_env is None else trust_env
        # explicit per-phase timeouts: a stalled stream between bytes must error,
        # not hang. `read` bounds the gap between streamed chunks.
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=20.0, read=timeout),
            trust_env=self.trust_env,
        )
        # hard wall-clock ceiling on a single chat() — even a slow-trickle stream
        # that evades the per-read timeout cannot wedge a solver past this.
        self.overall_timeout = overall_timeout

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "LLMClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _endpoint(self) -> str:
        """Absolute endpoint for the configured dialect, tolerating any base_url."""

        return _resolve_endpoint(self.base_url, self.wire_api)

    def _outbound(self, body: dict) -> dict:
        if self.wire_api == _RESPONSES:
            return _to_responses_request(body)
        return body

    def _inbound(self, data: dict) -> dict:
        """Normalise a reply into the Chat Completions shape callers parse."""

        if self.wire_api == _RESPONSES:
            return _responses_to_chat_body(data)
        return data

    async def _iter_chat_chunks(self, body: dict) -> "AsyncIterator[dict]":
        """Yield Chat-shaped stream chunks from whichever wire is configured.

        Shared by the two streaming readers so a Responses-only relay needs no
        second parser.  ``body`` is the Chat-shaped request; the translation to
        the configured dialect happens here.
        """

        if self.wire_api == _RESPONSES:
            async for chunk in self._iter_responses_chunks(body):
                yield chunk
            return
        async with self._client.stream(
            "POST", self._endpoint(), headers=self._headers(), json=body
        ) as r:
            if r.status_code >= 400:
                await r.aread()
                _raise_for_status(r)
            async for line in r.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[len("data:") :].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                yield chunk

    async def _iter_responses_chunks(self, body: dict) -> "AsyncIterator[dict]":
        """Fold the Responses event sequence into Chat-shaped stream chunks."""

        saw_tool_call = False
        async with self._client.stream(
            "POST",
            self._endpoint(),
            headers=self._headers(),
            json=_to_responses_request(body),
        ) as r:
            if r.status_code >= 400:
                await r.aread()
                _raise_for_status(r)
            async for line in r.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[len("data:") :].strip()
                if not data_str or data_str == "[DONE]":
                    continue
                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                kind = str(event.get("type") or "")
                if kind == "response.output_text.delta":
                    yield {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": event.get("delta") or ""},
                                "finish_reason": None,
                            }
                        ]
                    }
                elif kind in {
                    "response.reasoning_text.delta",
                    "response.reasoning_summary_text.delta",
                }:
                    yield {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"reasoning_content": event.get("delta") or ""},
                                "finish_reason": None,
                            }
                        ]
                    }
                elif kind == "response.output_item.added":
                    item = event.get("item") if isinstance(event.get("item"), dict) else {}
                    if str(item.get("type") or "") == "function_call":
                        saw_tool_call = True
                        yield {
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": int(event.get("output_index") or 0),
                                                "id": str(item.get("call_id") or ""),
                                                "type": "function",
                                                "function": {
                                                    "name": str(item.get("name") or ""),
                                                    "arguments": "",
                                                },
                                            }
                                        ]
                                    },
                                    "finish_reason": None,
                                }
                            ]
                        }
                elif kind == "response.function_call_arguments.delta":
                    yield {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": int(event.get("output_index") or 0),
                                            "function": {
                                                "arguments": str(event.get("delta") or "")
                                            },
                                        }
                                    ]
                                },
                                "finish_reason": None,
                            }
                        ]
                    }
                elif kind in {"response.completed", "response.incomplete", "response.failed"}:
                    response = event.get("response") if isinstance(event.get("response"), dict) else {}
                    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
                    yield {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": "tool_calls" if saw_tool_call else "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": int(usage.get("input_tokens") or 0),
                            "completion_tokens": int(usage.get("output_tokens") or 0),
                        },
                    }

    async def _emit(self, etype: EventType, *, run_id, challenge_id, solver_id, **payload):
        if self.bus is not None and run_id is not None:
            await self.bus.emit(
                Event(
                    event_type=etype,
                    run_id=run_id,
                    challenge_id=challenge_id,
                    solver_id=solver_id,
                    payload=payload,
                )
            )

    async def _record_cost(self, model, usage, run_id, challenge_id, solver_id):
        if self.cost is not None and run_id is not None and usage:
            await self.cost.record(
                model=model,
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                run_id=run_id,
                challenge_id=challenge_id,
                solver_id=solver_id,
            )

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: float = 0.4,
        max_tokens: Optional[int] = 8000,
        stream: bool = True,
        run_id: Optional[str] = None,
        challenge_id: Optional[str] = None,
        solver_id: Optional[str] = None,
    ) -> LLMResponse:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": stream,
        }
        # max_tokens=None → omit the cap entirely (let the API use the model's own
        # maximum). Critical for reasoning models (deepseek-v4-pro): tokens go to
        # reasoning_content FIRST, so a small cap can be fully consumed by thinking
        # and truncate the actual answer. The Reason planner relies on this.
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        coro = (
            self._chat_stream(body, run_id=run_id, challenge_id=challenge_id, solver_id=solver_id)
            if stream
            else self._chat_once(body, run_id=run_id, challenge_id=challenge_id, solver_id=solver_id)
        )
        # hard wall-clock guard: a stalled/half-open SSE stream must not wedge the
        # caller forever. On timeout we surface it as a normal error the solver
        # loop can recover from (treated like an empty turn), not a hang.
        try:
            resp = await asyncio.wait_for(coro, timeout=self.overall_timeout)
        except asyncio.TimeoutError:
            return LLMResponse(
                content="", reasoning="", tool_calls=[],
                finish_reason="timeout", model=body["model"],
            )
        return resp

    async def _chat_once(self, body, *, run_id, challenge_id, solver_id) -> LLMResponse:
        body = {**body, "stream": False}
        if body.get("stream") is True:  # safety
            body["stream"] = False
        r = await self._client.post(
            self._endpoint(), headers=self._headers(), json=self._outbound(body)
        )
        _raise_for_status(r)
        data = self._inbound(r.json())
        choice = data["choices"][0]
        msg = choice["message"]
        usage = data.get("usage", {})
        await self._record_cost(body["model"], usage, run_id, challenge_id, solver_id)

        tool_calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc["function"]
            tool_calls.append(
                ToolCall(id=tc.get("id", ""), name=fn["name"], arguments=fn.get("arguments", ""))
            )
        reasoning = msg.get("reasoning_content") or ""
        content = msg.get("content") or ""
        if reasoning:
            await self._emit(
                EventType.REASONING_DELTA,
                run_id=run_id, challenge_id=challenge_id, solver_id=solver_id, text=reasoning,
            )
        if content:
            await self._emit(
                EventType.TEXT_MESSAGE_DELTA,
                run_id=run_id, challenge_id=challenge_id, solver_id=solver_id, text=content,
            )
        return LLMResponse(
            content=content,
            reasoning=reasoning,
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason", ""),
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            model=body["model"],
        )

    async def _chat_stream(self, body, *, run_id, challenge_id, solver_id) -> LLMResponse:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        # tool calls reassembled by index
        tc_acc: dict[int, dict[str, str]] = {}
        finish_reason = ""
        usage: dict[str, int] = {}

        async for chunk in self._iter_chat_chunks(body):
            if chunk.get("usage"):
                usage = chunk["usage"]
            choices = chunk.get("choices") or []
            if not choices:
                continue
            ch = choices[0]
            delta = ch.get("delta") or {}
            if ch.get("finish_reason"):
                finish_reason = ch["finish_reason"]

            rc = delta.get("reasoning_content")
            if rc:
                reasoning_parts.append(rc)
                await self._emit(
                    EventType.REASONING_DELTA,
                    run_id=run_id, challenge_id=challenge_id, solver_id=solver_id, text=rc,
                )
            cc = delta.get("content")
            if cc:
                content_parts.append(cc)
                await self._emit(
                    EventType.TEXT_MESSAGE_DELTA,
                    run_id=run_id, challenge_id=challenge_id, solver_id=solver_id, text=cc,
                )
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tc_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]

        await self._record_cost(body["model"], usage, run_id, challenge_id, solver_id)

        tool_calls = [
            ToolCall(id=v["id"], name=v["name"], arguments=v["arguments"])
            for _, v in sorted(tc_acc.items())
            if v["name"]
        ]
        return LLMResponse(
            content="".join(content_parts),
            reasoning="".join(reasoning_parts),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            model=body["model"],
        )

    async def iter_chat_deltas(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.3,
        max_tokens: Optional[int] = 4000,
        run_id: Optional[str] = None,
        challenge_id: Optional[str] = None,
        solver_id: Optional[str] = None,
        emit: bool = False,
        record_cost: bool = False,
    ) -> "AsyncIterator[str]":
        """Stream content deltas from a chat completion, yielding one string chunk
        at a time. Designed for the read-only btw observer: by default it does NOT
        emit REASONING_DELTA/TEXT_MESSAGE_DELTA to the run bus and does NOT record
        cost into the run CostController — so a btw Q&A never pollutes the SSE event
        stream, the deck's DeckState, or the run cost/budget.

        `emit=True`/`record_cost=True` re-enable those side effects for non-btw
        callers. Reasoning deltas are NOT yielded (only `content` is), matching the
        observer UX which shows only the final answer text streaming in.

        Never raises mid-stream on parse errors: a malformed SSE line is skipped.
        Transport/HTTP errors propagate so the caller can render an error frame.
        The caller is responsible for aclose() (typically via `async with`).
        """
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        usage: dict[str, int] = {}
        async for chunk in self._iter_chat_chunks(body):
            if chunk.get("usage"):
                usage = chunk["usage"]
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            rc = delta.get("reasoning_content")
            if rc and emit:
                await self._emit(
                    EventType.REASONING_DELTA,
                    run_id=run_id, challenge_id=challenge_id,
                    solver_id=solver_id, text=rc,
                )
            cc = delta.get("content")
            if cc:
                if emit:
                    await self._emit(
                        EventType.TEXT_MESSAGE_DELTA,
                        run_id=run_id, challenge_id=challenge_id,
                        solver_id=solver_id, text=cc,
                    )
                yield cc
        if record_cost:
            await self._record_cost(body["model"], usage, run_id, challenge_id, solver_id)
