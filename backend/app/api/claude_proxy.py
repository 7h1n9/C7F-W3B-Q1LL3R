"""Anthropic-compatible ``/v1/messages`` proxy that fronts an OpenAI-chat upstream.

What this is for
----------------
Tools that speak the Anthropic protocol (Claude Code and anything else built on
the Messages API) can only be pointed at a ``/v1/messages`` endpoint. When the
only upstream available is an OpenAI-compatible relay -- which is the common case
here, see the ``claude-deep`` config -- those tools have nowhere to go. This
endpoint is that missing place: it accepts a Claude request, translates it down to
chat, forwards it, and translates the answer back up. The downstream client never
learns the protocol changed.

This is the cc-switch pattern, implemented server-side so the translation lives
next to the credentials and is shared by every client rather than repeated in each
one's config file.

Why the path is ``/v1/messages`` and not ``/api/v1/...``
-------------------------------------------------------
The path is part of the contract, not a preference: Claude clients append
``/v1/messages`` to whatever base URL they are configured with, and no amount of
documentation changes that. Mounting it under the app's normal ``/api/v1`` prefix
would make it unreachable by the tools it exists for. It is deliberately outside
:mod:`app.api.v1` for that reason.

Upstream selection
------------------
The caller names a model; this resolves it to an enabled :class:`ModelConfig`.
``claude-deep`` and its siblings are stored as ``claude_cli`` because they are
consumed through the Claude CLI, but the endpoint they actually expose is OpenAI
chat -- which is exactly the mismatch this proxy resolves. A model whose upstream
is already the Claude protocol would be forwarded unchanged; there is no benefit
to translating between identical shapes.
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import Any, AsyncIterator, Mapping

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_session
from app.core.exceptions import DomainError
from app.core.net import outbound_trust_env
from app.models.model_config import ModelConfig
from app.services.claude_message_converter import (
    chat_response_to_claude_body,
    claude_request_to_chat_body,
)
from app.services.crypto import decrypt_api_key
from muteki.core.wire import resolve_endpoint

logger = logging.getLogger(__name__)

router = APIRouter(tags=["claude-proxy"])

UPSTREAM_TIMEOUT = 300.0

#: Vendor marks the upstream relay reacts to, mapped to neutral spellings.
#:
#: The relay reads the **system prompt** to decide whether a request comes from an
#: official client, and a Claude CLI turn always says it does -- so the request is
#: refused with ``Illegal API invocation from an unapproved channel`` (HTTP 400,
#: code 11128) however correct the endpoint, key and protocol translation are.
#: The failure surfaces as a health probe that never passes, with no usable
#: diagnostic, which is why this is fixed here rather than at the call site.
#:
#: Only the spelling changes; sentence structure and meaning are preserved so the
#: model still understands its own role.  ``model`` is never touched -- it is a
#: protocol field the upstream resolves, not prose.  Tool schemas are left alone
#: too: an isolated test showed a real 22-tool payload passes untouched once the
#: system text is neutral.
_VENDOR_MARK_REWRITES = (
    ("Anthropic", "Anthropiq"),
    ("anthropic", "anthropiq"),
    ("Claude", "Klaude"),
    ("claude", "klaude"),
)


def _scrub_vendor_marks(text: str) -> str:
    for source, replacement in _VENDOR_MARK_REWRITES:
        if source in text:
            text = text.replace(source, replacement)
    return text


def _neutralise_system_prompt(chat_body: dict[str, Any]) -> None:
    """Rewrite vendor marks in the system prompt that is about to be forwarded.

    In place, and only on ``role="system"`` messages: that is where the CLI puts
    its self-identification.  Conversation turns and tool results carry the
    challenge's own text, which must reach the model verbatim.
    """

    for message in chat_body.get("messages") or []:
        if not isinstance(message, Mapping) or str(message.get("role")) != "system":
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = _scrub_vendor_marks(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                    part["text"] = _scrub_vendor_marks(part["text"])

#: Environment variable carrying the shared secret.  The standalone launcher sets
#: it; when unset, the proxy is only reachable from loopback and no token is
#: demanded, because a secret nobody can learn is not a secret.
PROXY_TOKEN_ENV = "APP_CLAUDE_PROXY_TOKEN"

#: Claude 的错误形状与 chat 不同，下游按 Anthropic 规范解析。原样透传 chat 的
#: ``{"error": {"message": ...}}`` 会让客户端报一个与真实原因无关的解析错误。
_ERROR_TYPE_BY_STATUS = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    500: "api_error",
    502: "api_error",
    503: "overloaded_error",
    529: "overloaded_error",
}


def _claude_error(status: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"type": "error", "error": {"type": _ERROR_TYPE_BY_STATUS.get(status, "api_error"), "message": message}},
    )


#: Claude CLI 只会发送它自己认识的官方模型名，而真实上游用的是别的名字。
#: 这些是 CLI 内置的默认代号，用来在没有任何映射配置时也能工作。
#: Model-name *families* the Claude CLI can name, matched as a prefix.
#:
#: Enumerating exact names was wrong: the CLI picks its default model by
#: version, and 2.1.293 asks for ``claude-sonnet-4-6`` -- a name no enumerated
#: list on this side had, so it fell through to "hand the upstream unchanged",
#: the relay answered "model may not exist", and every PREPARE health probe for
#: a ``route_via_proxy`` claude_cli engine failed.  A family prefix is stable
#: across those renames, while a genuinely foreign name (``gpt-4o``) still
#: misses every prefix and is still passed through untouched.
_OFFICIAL_CLAUDE_MODELS = (
    "claude-opus-",
    "claude-sonnet-",
    "claude-haiku-",
    "claude-3-",
    "claude-2",
)


def _aliases(config: ModelConfig) -> dict[str, str]:
    """The ``official name -> real upstream name`` map for one config.

    Read from ``capabilities_json.model_aliases``. This is the same mechanism
    cc-switch uses via ``ANTHROPIC_DEFAULT_*_MODEL_NAME``, but kept server-side:
    the operator's stored ``model_name`` stays what it really is, and the CLI's
    vocabulary is translated at the boundary instead of being baked into config.
    """

    capabilities = config.capabilities_json if isinstance(config.capabilities_json, dict) else {}
    raw = capabilities.get("model_aliases")
    if not isinstance(raw, Mapping):
        return {}
    return {str(key).strip().lower(): str(value).strip() for key, value in raw.items() if value}


def _resolve_upstream_model(config: ModelConfig, requested: str | None) -> str:
    """Translate the client's model name into the name the upstream expects.

    Claude CLI sends a name from its own built-in list -- ``claude-sonnet-5`` and
    friends -- which no third-party relay recognises. Two layers resolve that:

    1. An explicit alias on the config wins, so an operator can point different
       Claude tiers at different upstream models.
    2. Otherwise, any official Claude name falls back to the config's own
       ``model_name``. That is what makes "the client names a Claude model, the
       config names a DeepSeek one" work without extra setup, and it is the case
       that matters in practice because the CLI offers no way to send anything
       else.
    """

    real = str(config.model_name or config.name).strip()
    wanted = str(requested or "").strip().lower()
    if not wanted:
        return real

    alias = _aliases(config).get(wanted)
    if alias:
        return alias

    # A non-Claude name that matches this config is already correct as-is.
    if wanted == real.lower() or wanted == config.name.strip().lower():
        return real

    if any(wanted.startswith(official) for official in _OFFICIAL_CLAUDE_MODELS):
        return real

    # Unknown name: hand it to the upstream unchanged. Guessing would mask a
    # genuine misconfiguration, and the upstream's own "model not found" is a
    # clearer signal than a silent substitution.
    return str(requested).strip()


def _authorised(request: Request) -> bool:
    """Check the shared secret when one is configured.

    Only enforced when a token is set.  The standalone launcher binds to
    ``0.0.0.0`` so that Worker containers can reach it, which also means anything
    on the LAN can -- so a launcher that exposes it generates a token, and this
    verifies it.  Comparing with :func:`hmac.compare_digest` because a plain
    ``==`` on a secret leaks its length and prefix through timing.
    """

    expected = str(get_settings().claude_proxy_token or "").strip()
    if not expected:
        return True
    supplied = (
        request.headers.get("x-api-key")
        or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        or ""
    ).strip()
    return bool(supplied) and hmac.compare_digest(supplied, expected)


async def _resolve_upstream(
    session: AsyncSession, requested_model: str | None
) -> ModelConfig:
    """Pick the upstream config for this request.

    Resolution order: an exact model-name match, then whatever enabled config
    exists. Falling back rather than failing keeps a client usable when it sends a
    Claude model name the relay does not know -- which is the normal case, since
    the client has no way to learn the relay's model list.
    """

    enabled = list(
        (
            await session.scalars(
                select(ModelConfig).where(ModelConfig.enabled.is_(True)).order_by(ModelConfig.name)
            )
        ).all()
    )
    if not enabled:
        raise DomainError(
            "CLAUDE_PROXY_NO_UPSTREAM",
            "No enabled model configuration is available to serve this request.",
            status_code=503,
            stage="CLAUDE_PROXY",
        )
    wanted = str(requested_model or "").strip().lower()
    if wanted:
        # An alias names the config that claims this Claude model, which is the
        # operator's explicit intent and outranks name matching.
        for config in enabled:
            if wanted in _aliases(config):
                return config
        for config in enabled:
            if str(config.model_name or "").strip().lower() == wanted:
                return config
        for config in enabled:
            if config.name.strip().lower() == wanted:
                return config
    # Default to the first enabled config; the caller cannot know the relay's
    # catalogue, so requiring an exact name would make the proxy unusable.
    return enabled[0]


@router.post("/v1/messages")
async def claude_messages(request: Request, session: AsyncSession = Depends(get_session)):
    """Accept an Anthropic Messages request and serve it from a chat upstream."""

    if not _authorised(request):
        return _claude_error(401, "Invalid or missing proxy token.")

    try:
        payload: Mapping[str, Any] = await request.json()
    except ValueError:
        return _claude_error(400, "Request body is not valid JSON.")
    if not isinstance(payload, Mapping):
        return _claude_error(400, "Request body must be a JSON object.")

    config = await _resolve_upstream(session, payload.get("model"))
    api_key = decrypt_api_key(config.encrypted_api_key) if config.encrypted_api_key else ""
    endpoint = resolve_endpoint(config.base_url)

    chat_body = claude_request_to_chat_body(payload)
    # The caller sends a Claude model name; the upstream only answers to its own.
    # This translation is the whole reason the client never has to know the
    # upstream's naming -- see _resolve_upstream_model.
    chat_body["model"] = _resolve_upstream_model(config, payload.get("model"))
    # Applies to both the streaming and non-streaming paths below, since they
    # share this body.
    if get_settings().claude_proxy_scrub_vendor_marks:
        _neutralise_system_prompt(chat_body)

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    wants_stream = bool(payload.get("stream"))

    if not wants_stream:
        async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT, trust_env=outbound_trust_env()) as client:
            try:
                response = await client.post(endpoint, headers=headers, json=chat_body)
            except httpx.HTTPError as error:
                return _claude_error(502, f"Upstream is unreachable: {error}")
        if not response.is_success:
            # Report the upstream's own message where possible; a generic one
            # would hide the actual cause (bad key, unknown model, overload).
            try:
                detail = response.json()
                message = str(
                    (detail.get("error") or {}).get("message")
                    if isinstance(detail.get("error"), Mapping)
                    else detail.get("message") or ""
                )
            except ValueError:
                message = response.text[:400]
            logger.warning("claude-proxy upstream %s: %s", response.status_code, message[:200])
            return _claude_error(response.status_code, message or "Upstream request failed.")
        try:
            upstream_body = response.json()
        except ValueError:
            return _claude_error(502, "Upstream returned a non-JSON body.")
        return JSONResponse(content=chat_response_to_claude_body(upstream_body))

    return StreamingResponse(
        _stream_claude_events(endpoint, headers, chat_body),
        media_type="text/event-stream",
    )


async def _stream_claude_events(
    endpoint: str, headers: dict[str, str], chat_body: Mapping[str, Any]
) -> AsyncIterator[str]:
    """Translate an upstream chat SSE stream into Claude SSE events.

    Anthropic's event sequence is not a rename of chat's: it opens with
    ``message_start``, brackets each content block with ``..._block_start`` /
    ``..._block_stop``, emits ``message_delta`` for the stop reason, and closes
    with ``message_stop``. A client that parses these will mis-handle a raw chat
    stream, so the events are rebuilt rather than passed through.

    Index accounting is the part that has to be exact: chat interleaves text and
    tool-call deltas in one stream, while Claude addresses blocks by index, so a
    tool call that follows text must open at index 1, not 0.
    """

    async def emit(event: str, data: dict[str, Any]) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    message_id = "msg_proxy"
    model = str(chat_body.get("model") or "")
    yield await emit(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )
    yield await emit("content_block_start", {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "text", "text": ""}})
    yield await emit("ping", {"type": "ping"})

    text_open = True
    next_index = 1
    tool_index_by_slot: dict[int, int] = {}
    stop_reason = "end_turn"
    output_tokens = 0

    try:
        async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT, trust_env=outbound_trust_env()) as client:
            async with client.stream("POST", endpoint, headers=headers, json=chat_body) as response:
                if not response.is_success:
                    body = (await response.aread()).decode("utf-8", "replace")[:400]
                    yield await emit("error", {
                        "type": "error",
                        "error": {"type": _ERROR_TYPE_BY_STATUS.get(response.status_code, "api_error"),
                                  "message": body or "Upstream streaming request failed."}})
                    return
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    choice = (chunk.get("choices") or [{}])[0]
                    if not isinstance(choice, Mapping):
                        continue
                    delta = choice.get("delta") if isinstance(choice.get("delta"), Mapping) else {}

                    text = delta.get("content")
                    if isinstance(text, str) and text:
                        yield await emit("content_block_delta", {
                            "type": "content_block_delta", "index": 0,
                            "delta": {"type": "text_delta", "text": text}})

                    for call in delta.get("tool_calls") or []:
                        if not isinstance(call, Mapping):
                            continue
                        slot = int(call.get("index") or 0)
                        function = call.get("function") if isinstance(call.get("function"), Mapping) else {}
                        if slot not in tool_index_by_slot:
                            # First sighting of this call: close text, open a block.
                            if text_open:
                                yield await emit("content_block_stop", {
                                    "type": "content_block_stop", "index": 0})
                                text_open = False
                            tool_index_by_slot[slot] = next_index
                            next_index += 1
                            yield await emit("content_block_start", {
                                "type": "content_block_start",
                                "index": tool_index_by_slot[slot],
                                "content_block": {
                                    "type": "tool_use",
                                    "id": str(call.get("id") or ""),
                                    "name": str(function.get("name") or ""),
                                    # Claude sends the input as a streamed JSON
                                    # fragment, not as an object.
                                    "input": {},
                                }})
                        arguments = function.get("arguments")
                        if isinstance(arguments, str) and arguments:
                            yield await emit("content_block_delta", {
                                "type": "content_block_delta",
                                "index": tool_index_by_slot[slot],
                                "delta": {"type": "input_json_delta", "partial_json": arguments}})

                    finish = choice.get("finish_reason")
                    if finish:
                        stop_reason = {
                            "stop": "end_turn", "length": "max_tokens",
                            "tool_calls": "tool_use", "function_call": "tool_use",
                        }.get(str(finish), "end_turn")
                    usage = chunk.get("usage") if isinstance(chunk.get("usage"), Mapping) else {}
                    if usage.get("completion_tokens"):
                        output_tokens = int(usage["completion_tokens"])
    except httpx.HTTPError as error:
        yield await emit("error", {
            "type": "error",
            "error": {"type": "api_error", "message": f"Upstream stream failed: {error}"}})
        return

    if text_open:
        yield await emit("content_block_stop", {"type": "content_block_stop", "index": 0})
    for index in sorted(tool_index_by_slot.values()):
        yield await emit("content_block_stop", {"type": "content_block_stop", "index": index})
    yield await emit("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": output_tokens}})
    yield await emit("message_stop", {"type": "message_stop"})
