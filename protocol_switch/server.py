"""HTTP entry point for the protocol switch service.

Exposes the three model dialects on the paths the real clients use:

===========================  ==========================================
``POST /v1/chat/completions``  OpenAI Chat Completions
``POST /v1/responses``         OpenAI Responses (what Codex CLI speaks)
``POST /v1/messages``          Anthropic Messages (what Claude Code speaks)
===========================  ==========================================

Each endpoint translates its request into the upstream dialect, forwards it,
and translates the answer back -- streaming or buffered.  When the inbound
dialect already matches the upstream, the body and the stream are relayed
untouched so nothing is lost in a round trip that did not need one.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Mapping

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .config import Settings
from .stream import atransform
from .translate import (
    CHAT,
    MESSAGES,
    RESPONSES,
    decode,
    encode,
    parse_answer,
    render,
    resolve_endpoint,
)

#: Dialects whose SSE frames use an ``event:`` line.  Chat Completions does not.
_SSE_CONTENT_TYPE = "text/event-stream; charset=utf-8"

_ERROR_TYPE_BY_STATUS = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    429: "rate_limit_error",
}


def _error_body(dialect: str, status: int, message: str) -> dict[str, Any]:
    """Render an upstream failure in the caller's own error dialect."""

    kind = _ERROR_TYPE_BY_STATUS.get(status, "api_error")
    if dialect == MESSAGES:
        return {"type": "error", "error": {"type": kind, "message": message}}
    return {"error": {"message": message, "type": kind, "code": status}}


def _authorised(settings: Settings, request: Request) -> bool:
    if not settings.inbound_api_key:
        return True
    header = request.headers.get("authorization") or ""
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not token:
        token = (request.headers.get("x-api-key") or "").strip()
    return token == settings.inbound_api_key


def _upstream_headers(settings: Settings) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {settings.upstream_api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    headers.update(settings.upstream_headers)
    return headers


async def _forward_buffered(
    client: httpx.AsyncClient,
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
) -> tuple[int, str]:
    response = await client.post(url, headers=dict(headers), json=dict(payload))
    return response.status_code, response.text


def _extract_message(body: str) -> str:
    try:
        parsed = json.loads(body)
    except ValueError:
        return body.strip()[:500] or "upstream returned an empty body"
    if isinstance(parsed, Mapping):
        error = parsed.get("error")
        if isinstance(error, Mapping):
            return str(error.get("message") or error)[:500]
        if isinstance(error, str):
            return error[:500]
        if parsed.get("message"):
            return str(parsed["message"])[:500]
    return body.strip()[:500]


def create_app(
    settings: Settings | None = None,
    *,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the service.

    ``upstream_transport`` is a test seam: injecting an ``httpx.MockTransport``
    lets the whole request/response path (including SSE) be exercised without a
    live relay, which is the only way to regression-test the streaming
    translators end to end.
    """

    resolved = settings or Settings.from_env()
    state: dict[str, Any] = {"settings": resolved, "client": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        state["client"] = httpx.AsyncClient(
            timeout=httpx.Timeout(
                resolved.timeout_seconds, connect=30.0, read=resolved.timeout_seconds
            ),
            trust_env=False,
            follow_redirects=False,
            transport=upstream_transport,
        )
        try:
            yield
        finally:
            client: httpx.AsyncClient = state["client"]
            await client.aclose()
            state["client"] = None

    app = FastAPI(
        title="Protocol Switch",
        version="1.0.0",
        description="Bidirectional Chat Completions / Responses / Messages translation.",
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "service": "protocol-switch", **resolved.describe()}

    @app.get("/v1/models")
    async def models(request: Request) -> Response:
        if not _authorised(resolved, request):
            return JSONResponse(_error_body(CHAT, 401, "invalid switch API key"), 401)
        if not resolved.upstream_base_url:
            return JSONResponse({"object": "list", "data": []}, 200)
        root = resolved.upstream_base_url.rstrip("/")
        for path in ("/chat/completions", "/responses", "/completions", "/messages"):
            if root.endswith(path):
                root = root[: -len(path)]
                break
        client: httpx.AsyncClient = state["client"]
        try:
            upstream = await client.get(
                f"{root}/models", headers={"Authorization": f"Bearer {resolved.upstream_api_key}"}
            )
        except httpx.HTTPError as error:
            return JSONResponse(_error_body(CHAT, 502, str(error)[:300]), 502)
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
        )

    async def handle(request: Request, dialect: str) -> Response:
        if not _authorised(resolved, request):
            return JSONResponse(_error_body(dialect, 401, "invalid switch API key"), 401)
        if not resolved.upstream_base_url:
            return JSONResponse(
                _error_body(
                    dialect,
                    503,
                    "SWITCH_UPSTREAM_BASE_URL is not configured",
                ),
                503,
            )
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(_error_body(dialect, 400, "request body is not JSON"), 400)
        if not isinstance(body, Mapping):
            return JSONResponse(_error_body(dialect, 400, "request body must be an object"), 400)

        canonical = decode(dialect, body)
        original_model = str(body.get("model") or "").strip()
        mapped_model = resolved.map_model(original_model)
        canonical.params.model = mapped_model or original_model
        stream_requested = bool(canonical.params.stream)
        headers = _upstream_headers(resolved)
        client: httpx.AsyncClient = state["client"]

        if dialect == resolved.upstream_wire:
            # Same dialect both sides: relay verbatim.  A translation round trip
            # could only lose fields (vendor extensions, tool schemas, logprobs).
            # The model alias table still applies, so rewrite it in place rather
            # than skipping the mapping just because nothing else changes.
            outbound: Mapping[str, Any] = (
                body
                if not mapped_model or mapped_model == original_model
                else {**body, "model": mapped_model}
            )
            url = resolve_endpoint(resolved.upstream_base_url, dialect)
            if not stream_requested:
                try:
                    status, text = await _forward_buffered(client, url, headers, outbound)
                except httpx.HTTPError as error:
                    return JSONResponse(_error_body(dialect, 502, str(error)[:300]), 502)
                if status >= 400:
                    return JSONResponse(_error_body(dialect, status, _extract_message(text)), status)
                return Response(
                    content=text, status_code=status, media_type="application/json"
                )
            return await _relay_stream(
                client, url, headers, outbound, dialect, resolved.timeout_seconds
            )

        upstream_body = encode(resolved.upstream_wire, canonical)
        upstream_body["stream"] = stream_requested
        url = resolve_endpoint(resolved.upstream_base_url, resolved.upstream_wire)
        model = canonical.params.model

        if not stream_requested:
            try:
                status, text = await _forward_buffered(client, url, headers, upstream_body)
            except httpx.HTTPError as error:
                return JSONResponse(_error_body(dialect, 502, str(error)[:300]), 502)
            if status >= 400:
                return JSONResponse(_error_body(dialect, status, _extract_message(text)), status)
            try:
                upstream_json = json.loads(text)
            except ValueError:
                return JSONResponse(
                    _error_body(dialect, 502, "upstream returned a non-JSON body"), 502
                )
            if not isinstance(upstream_json, Mapping):
                return JSONResponse(
                    _error_body(dialect, 502, "upstream returned an unexpected body"), 502
                )
            answer = parse_answer(resolved.upstream_wire, upstream_json)
            if not answer.model:
                answer.model = model
            return JSONResponse(render(dialect, answer))

        return await _translated_stream(
            client,
            url,
            headers,
            upstream_body,
            inbound=dialect,
            upstream=resolved.upstream_wire,
            model=model,
        )

    async def _relay_stream(
        client: httpx.AsyncClient,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        dialect: str,
        timeout: float,
    ) -> Response:
        """Stream an upstream response through without re-encoding it."""

        request = client.build_request(
            "POST", url, headers=dict(headers), json=dict(payload), timeout=timeout
        )
        upstream = await client.send(request, stream=True)
        if upstream.status_code >= 400:
            body = (await upstream.aread()).decode("utf-8", "replace")
            await upstream.aclose()
            return JSONResponse(
                _error_body(dialect, upstream.status_code, _extract_message(body)),
                upstream.status_code,
            )

        async def relay() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(
            relay(),
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", _SSE_CONTENT_TYPE),
        )

    async def _translated_stream(
        client: httpx.AsyncClient,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        *,
        inbound: str,
        upstream: str,
        model: str,
    ) -> Response:
        request = client.build_request("POST", url, headers=dict(headers), json=dict(payload))
        upstream_response = await client.send(request, stream=True)
        if upstream_response.status_code >= 400:
            body = (await upstream_response.aread()).decode("utf-8", "replace")
            await upstream_response.aclose()
            return JSONResponse(
                _error_body(inbound, upstream_response.status_code, _extract_message(body)),
                upstream_response.status_code,
            )

        async def lines() -> AsyncIterator[str]:
            try:
                async for chunk in upstream_response.aiter_text():
                    yield chunk
            finally:
                await upstream_response.aclose()

        async def frames() -> AsyncIterator[str]:
            async for frame in atransform(
                upstream=upstream, downstream=inbound, chunks=lines(), model=model
            ):
                yield frame

        return StreamingResponse(frames(), media_type=_SSE_CONTENT_TYPE)

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        return await handle(request, CHAT)

    @app.post("/v1/responses")
    async def responses(request: Request) -> Response:
        return await handle(request, RESPONSES)

    @app.post("/v1/messages")
    async def messages(request: Request) -> Response:
        return await handle(request, MESSAGES)

    return app


app = create_app()


__all__ = ["app", "create_app"]
