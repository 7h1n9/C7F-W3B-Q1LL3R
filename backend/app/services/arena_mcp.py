"""MCP client for the CTF2 Agent arena.

Why a second client exists
--------------------------
:mod:`app.services.arena_client` speaks the arena's public REST surface
(``/api/open/v1/user``), which exposes exactly two read routes.  The twelve
``ctf2_*`` tools are **not** REST endpoints -- they live behind the MCP endpoint
at ``/api/ai/v1/mcp``.  Both facts were established by probing the live API on
2026-10-12, and they are two different API surfaces, not two views of one.

So this module owns everything the REST client cannot do: reading a challenge in
full, fetching attachment links, and -- the part that decides whether a solve is
possible at all -- the environment lifecycle and flag submission.

Transport
---------
Streamable HTTP.  Three details are load-bearing and were each confirmed against
the live endpoint:

* ``Accept`` must contain **both** ``application/json`` and
  ``text/event-stream``; the server rejects anything narrower.
* ``initialize`` returns an ``Mcp-Session-Id`` header that every later call must
  echo back.  A client that drops it gets a fresh, unauthenticated session.
* Responses may be SSE frames rather than a bare JSON body, so the parser has to
  walk ``data:`` lines.  Assuming ``response.json()`` works fails intermittently,
  which is the worst way for it to fail.

Tool errors are **not** transport errors.  A tool that rejects its arguments
answers HTTP 200 with ``isError: true`` and a structured ``{code, message,
retryable, retry_after_ms, hint}`` payload.  Those are raised as
:class:`ToolError` so a caller can honour ``hint`` and ``retry_after_ms``
instead of retrying blindly -- the platform's own instructions require this.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from app.core.exceptions import DomainError
from app.core.net import outbound_trust_env

#: The MCP endpoint.  Distinct from the REST root in ``arena_client.PATHS``.
MCP_PATH = "/api/ai/v1/mcp"

#: Protocol revision.  The endpoint accepted this value on 2026-10-12.
PROTOCOL_VERSION = "2025-06-18"

INITIALIZE_TIMEOUT = 20.0
CALL_TIMEOUT = 60.0

#: Attachment links are signed and expire; the platform's instructions state five
#: minutes.  Recorded so a caller does not treat a URL as durable.
ATTACHMENT_URL_TTL_SECONDS = 300


class ToolError(DomainError):
    """A tool rejected the call.  Carries the platform's own recovery guidance."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        retry_after_ms: int | None = None,
        hint: str | None = None,
        status_code: int = 422,
    ) -> None:
        details: dict[str, Any] = {"hint": hint}
        if retry_after_ms is not None:
            details["retry_after_ms"] = retry_after_ms
        super().__init__(
            code or "ARENA_TOOL_REJECTED",
            message or "The arena rejected this tool call.",
            details,
            status_code,
            stage="ARENA_TOOL",
            retryable=retryable,
        )
        self.retry_after_ms = retry_after_ms
        self.hint = hint


def parse_mcp_response(body: str) -> dict:
    """Pull the JSON-RPC message out of a body that may be SSE-framed.

    A bare ``response.json()`` is not enough: the same endpoint answers a plain
    JSON body for some calls and an SSE stream for others, so both shapes have to
    be accepted or the client fails intermittently.
    """
    text = (body or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            return json.loads(text)
        except ValueError:
            pass
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return {}


class ArenaMcpClient:
    """One MCP session against one arena connection.

    Obtain instances from :data:`arena_mcp_clients` -- the session is stateful
    (initialize + session id) and the platform rate-limits per agent, so a second
    client for the same connection is both wasteful and a source of 429s.
    """

    def __init__(self, *, connection_id: str, base_url: str, token: str | None) -> None:
        self.connection_id = connection_id
        # The MCP endpoint hangs off the arena host, not off the REST base_url,
        # which already carries /api/open/v1/user.
        self._origin = base_url.rstrip("/").split("/api/", 1)[0]
        self._token = token
        self._session_id: str | None = None
        self._rpc_id = 0
        self._lock = asyncio.Lock()
        self._tools: list[dict] | None = None
        self._instructions: str = ""

    @property
    def endpoint(self) -> str:
        return f"{self._origin}{MCP_PATH}"

    @property
    def instructions(self) -> str:
        """The platform's own solve-loop guidance, verbatim (read-only)."""
        return self._instructions

    @property
    def tools(self) -> list[dict]:
        return list(self._tools or [])

    def _headers(self) -> dict[str, str]:
        if not self._token:
            raise DomainError(
                "ARENA_TOKEN_MISSING",
                "This arena connection has no token configured.",
                status_code=422,
            )
        headers = {
            "Authorization": f"Bearer {self._token}",
            # Both are required; the endpoint rejects a narrower Accept.
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    async def _post(self, payload: dict, *, timeout: float) -> dict:
        # Not ``trust_env=True``: see ``Settings.respect_proxy_env``.  Inheriting
        # a session-scoped egress proxy here is what turns a healthy batch into
        # ``ARENA_MCP_UNREACHABLE`` the moment that proxy is gone.
        async with httpx.AsyncClient(timeout=timeout, trust_env=outbound_trust_env()) as client:
            try:
                response = await client.post(self.endpoint, headers=self._headers(), json=payload)
            except httpx.HTTPError as error:
                raise DomainError(
                    "ARENA_MCP_UNREACHABLE",
                    f"Arena MCP endpoint is unreachable: {error}",
                    {"url": self.endpoint},
                    503,
                    stage="ARENA_MCP",
                    retryable=True,
                ) from error
        if not response.is_success:
            raise DomainError(
                "ARENA_MCP_HTTP_ERROR",
                f"Arena MCP endpoint answered HTTP {response.status_code}.",
                {"status_code": response.status_code},
                503 if response.status_code >= 500 else response.status_code,
                stage="ARENA_MCP",
                retryable=response.status_code == 429 or response.status_code >= 500,
            )
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id
        return parse_mcp_response(response.text)

    async def _ensure_session(self) -> None:
        """Initialize once.  Idempotent; the platform expires sessions server-side."""
        if self._session_id and self._tools is not None:
            return
        async with self._lock:
            if self._session_id and self._tools is not None:
                return
            body = await self._post(
                {
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": "initialize",
                    "params": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "c7f-w3b-q1ll3r", "version": "1.0"},
                    },
                },
                timeout=INITIALIZE_TIMEOUT,
            )
            result = body.get("result")
            if not isinstance(result, dict):
                error = body.get("error") or {}
                raise DomainError(
                    "ARENA_MCP_INIT_FAILED",
                    str(error.get("message") or "Arena MCP initialize failed."),
                    {"raw": body},
                    502,
                    stage="ARENA_MCP",
                )
            self._instructions = str(result.get("instructions") or "")
            # The notification carries no id and expects no reply; skipping it
            # leaves the session half-open and later calls may be refused.
            await self._post(
                {"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=CALL_TIMEOUT
            )
            listing = await self._post(
                {"jsonrpc": "2.0", "id": self._next_id(), "method": "tools/list"},
                timeout=CALL_TIMEOUT,
            )
            tools = ((listing.get("result") or {}).get("tools")) or []
            self._tools = [tool for tool in tools if isinstance(tool, dict)]

    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    async def call(self, name: str, arguments: dict | None = None) -> Any:
        """Invoke one tool and return its decoded payload.

        The platform returns tool results as a JSON string inside the MCP content
        envelope.  This unwraps that so callers get the data, not a string
        containing JSON -- and raises :class:`ToolError` when the tool itself
        refused, which is a different condition from a transport failure.
        """
        await self._ensure_session()
        body = await self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            },
            timeout=CALL_TIMEOUT,
        )
        if "error" in body and isinstance(body["error"], dict):
            error = body["error"]
            raise DomainError(
                "ARENA_MCP_ERROR",
                str(error.get("message") or "Arena MCP call failed."),
                {"tool": name, "code": error.get("code")},
                502,
                stage="ARENA_MCP",
            )
        result = body.get("result") or {}
        content = result.get("content") or []
        text = ""
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = str(part.get("text") or "")
                break

        if result.get("isError"):
            payload = _decode(text) or {}
            structured = result.get("structuredContent") if isinstance(result.get("structuredContent"), dict) else {}
            merged = {**structured, **(payload if isinstance(payload, dict) else {})}
            raise ToolError(
                str(merged.get("code") or "ARENA_TOOL_REJECTED"),
                str(merged.get("message") or text or "The arena rejected this tool call."),
                retryable=bool(merged.get("retryable")),
                retry_after_ms=merged.get("retry_after_ms"),
                hint=merged.get("hint"),
            )
        return _decode(text)

    # -- typed tool wrappers -------------------------------------------------
    # Only the calls the solve loop actually makes are wrapped.  A wrapper per
    # tool would be busywork; these are the ones whose arguments or return shape
    # the caller must get right.

    async def whoami(self) -> Any:
        return await self.call("ctf2_agent_whoami")

    async def list_practice_grounds(self, *, limit: int = 50, search: str | None = None) -> Any:
        args: dict[str, Any] = {"limit": limit}
        if search:
            args["search"] = search
        return await self.call("ctf2_list_practice_grounds", args)

    async def list_practice_challenges(
        self,
        *,
        practice_ground_id: str,
        category: str | None = None,
        difficulty: str | None = None,
        limit: int = 50,
        page: int | None = None,
        unsolved_only: bool = False,
    ) -> Any:
        """One page of a ground's catalogue.

        ``page`` is a real parameter, not a hopeful one: page 2 of BUUCTF-Real
        returns 100 challenges with zero overlap with page 1, and the response
        carries ``total`` and ``total_page`` alongside the items.  ``difficulty``
        and ``unsolved_only`` are likewise filtered **server-side**, which is what
        makes a ground of 3244 challenges usable -- filtering a single 100-item
        page locally silently answers "no match" for anything past page 1.
        """
        args: dict[str, Any] = {"practice_ground_id": practice_ground_id, "limit": limit}
        if category:
            args["category"] = category
        if difficulty:
            args["difficulty"] = difficulty
        if page:
            args["page"] = page
        if unsolved_only:
            args["unsolved_only"] = True
        return await self.call("ctf2_list_practice_challenges", args)

    async def get_practice_challenge(self, *, practice_ground_id: str, challenge_id: str) -> Any:
        # practice_ground_id is mandatory here even though the challenge id is
        # unique: omitting it is rejected with INVALID_REQUEST.
        return await self.call(
            "ctf2_get_practice_challenge",
            {"practice_ground_id": practice_ground_id, "challenge_id": challenge_id},
        )

    async def get_attachment_url(self, *, practice_ground_id: str, challenge_id: str, file_id: str) -> Any:
        return await self.call(
            "ctf2_get_attachment_url",
            {"practice_ground_id": practice_ground_id, "challenge_id": challenge_id, "file_id": file_id},
        )

    async def start_environment(self, *, practice_ground_id: str, challenge_id: str) -> Any:
        return await self.call(
            "ctf2_start_challenge_environment",
            {"practice_ground_id": practice_ground_id, "challenge_id": challenge_id},
        )

    async def get_environment(self, *, practice_ground_id: str, challenge_id: str) -> Any:
        return await self.call(
            "ctf2_get_environment",
            {"practice_ground_id": practice_ground_id, "challenge_id": challenge_id},
        )

    async def extend_environment(self, *, practice_ground_id: str, challenge_id: str) -> Any:
        return await self.call(
            "ctf2_extend_environment",
            {"practice_ground_id": practice_ground_id, "challenge_id": challenge_id},
        )

    async def delete_environment(self, *, practice_ground_id: str, challenge_id: str) -> Any:
        return await self.call(
            "ctf2_user_delete_practice_id_challenges_challengeid_environment",
            {"practice_ground_id": practice_ground_id, "challenge_id": challenge_id},
        )

    async def submit_flag(
        self, *, practice_ground_id: str, challenge_id: str, flag: str, sub_flag_id: str | None = None
    ) -> Any:
        # confirmation=true is required by the platform; a submission without it
        # is rejected rather than treated as a default.
        args: dict[str, Any] = {
            "practice_ground_id": practice_ground_id,
            "challenge_id": challenge_id,
            "flag": flag,
            "confirmation": True,
        }
        if sub_flag_id:
            args["sub_flag_id"] = sub_flag_id
        return await self.call("ctf2_submit_flag", args)

    async def log_note(self, *, challenge_id: str, content: str, kind: str = "finding") -> Any:
        """Record a note.  The caller must sanitise: the platform forbids flags here."""
        return await self.call(
            "ctf2_agent_log_note",
            {"challenge_id": challenge_id, "content": content, "kind": kind},
        )


def _decode(text: str) -> Any:
    """Decode a tool's text payload; fall back to the raw string."""
    stripped = (text or "").strip()
    if not stripped:
        return {}
    try:
        return json.loads(stripped)
    except ValueError:
        return {"raw": stripped}


class _McpRegistry:
    """One client (and therefore one MCP session) per connection."""

    def __init__(self) -> None:
        self._clients: dict[str, tuple[object, ArenaMcpClient]] = {}
        self._lock = asyncio.Lock()

    async def get(self, connection, token: str | None) -> ArenaMcpClient:
        fingerprint = (connection.base_url, connection.encrypted_token, connection.enabled)
        async with self._lock:
            cached = self._clients.get(connection.id)
            if cached and cached[0] == fingerprint:
                return cached[1]
            client = ArenaMcpClient(
                connection_id=connection.id, base_url=connection.base_url, token=token
            )
            self._clients[connection.id] = (fingerprint, client)
            return client

    def forget(self, connection_id: str) -> None:
        self._clients.pop(connection_id, None)

    def clear(self) -> None:
        self._clients.clear()


arena_mcp_clients = _McpRegistry()
