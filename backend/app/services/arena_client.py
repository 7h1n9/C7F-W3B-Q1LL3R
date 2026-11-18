"""HTTP client for the DASCTF Agent arena open API.

Single point of contact, on purpose
-----------------------------------
The client is cached per connection in :data:`arena_clients`.  That is not a
micro-optimisation: the platform rate-limits **per agent**, and an agent identity
is whatever the bearer token says it is.  Two modules each constructing their own
client therefore hold two independent pace limiters against one shared remote
quota, and the failure looks like random 429s rather than a bug.  Every consumer
must obtain the client from :func:`arena_clients.get`, never build it directly.

What the platform actually exposes
----------------------------------
This was established by probing the live API on 2026-10-12 with a real agent
token, not inferred from the tool names.  The result contradicts the naming:

* ``GET /agent/me/``                    -- agent profile, quotas, allowed_tools
* ``GET /agent/next-challenges/``       -- catalogue, with ``category`` / ``limit`` / ``offset``

That is the entire read surface of the ``/api/open/v1/user`` root.  Every other
guess -- ``practice-grounds``, ``practice-challenges``, a challenge detail by id,
an attachment URL route -- answers ``404 {"code":"NOT_FOUND","resource":"route"}``,
including with a trailing slash, as POST, and under every parent path tried.

Why: the platform's twelve ``ctf2_*`` tools are **MCP tools**, not REST routes.
Probing confirmed there is no MCP transport reachable from this host either
(``/mcp/`` and ``/sse`` are served by the SPA's nginx and return HTML on GET,
``405 Not Allowed`` on POST).  So an HTTP client cannot reach ten of the twelve
tools at all; they are only callable through whatever MCP/agent surface the
platform issues to a contestant.

The consequence for this module is deliberate and narrow: it implements the two
routes that exist, and it does **not** stub the other ten.  A stub that always
404s is worse than an absent method, because the former reads as a transient
failure and the latter as the architectural fact it is.

``next-challenges`` behaves as the catalogue
--------------------------------------------
It returns ``{"data": {"items": [...], "total": N}, "success": true}``.  Observed
behaviour, all verified against the live API:

* ``limit`` defaults to **10** and caps at **100**; a larger value is silently
  clamped back to 10 rather than rejected.
* ``offset`` is **ignored** -- paging past the first 100 returns the same first
  page.  Exactly 100 distinct challenges are therefore enumerable, and a caller
  must not believe it has seen the whole arena.
* ``category`` filters server-side (``WEB`` / ``CRYPTO`` / ``MISC`` / ``REVERSE``
  / ``PWN``).  It is free-form, not an enum: rows also carry chapter names such as
  ``第01章 web入门``, so the value is a label to display, never a value to match on
  exhaustively.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

import httpx

from app.core.exceptions import DomainError
from app.core.net import outbound_trust_env

#: The complete, verified read surface.  ``{}`` slots are ``str.format``
#: placeholders.  Anything not in this mapping is not reachable over HTTP --
#: see the module docstring; those tools live behind MCP.
PATHS: dict[str, str] = {
    "whoami": "/agent/me/",
    "challenges": "/agent/next-challenges/",
}

#: Platform limit semantics, verified by probing.  ``limit`` above the cap is
#: clamped to the default, not rejected, so a caller asking for 500 silently gets
#: 10 -- these constants exist so no caller has to rediscover that.
DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 100
#: ``offset`` does not page.  Recorded so the catalogue reader can stop after one
#: request instead of issuing offsets that all return page one.
OFFSET_SUPPORTED = False

REQUEST_TIMEOUT = 20.0
PROBE_TIMEOUT = 12.0


class _RateLimiter:
    """A sliding one-minute window plus a concurrency gate.

    The platform publishes no quota number, so the client paces itself from the
    configured ``requests_per_minute`` and treats a 429 as authoritative feedback
    rather than an impossible state.  Windows are per client, i.e. per connection.
    """

    def __init__(self, requests_per_minute: int, max_concurrency: int) -> None:
        self._rpm = max(1, requests_per_minute)
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self._stamps: deque[float] = deque()

    async def __aenter__(self) -> "_RateLimiter":
        await self._semaphore.acquire()
        while True:
            now = time.monotonic()
            while self._stamps and now - self._stamps[0] >= 60.0:
                self._stamps.popleft()
            if len(self._stamps) < self._rpm:
                self._stamps.append(now)
                return self
            # Wait exactly until the oldest stamp leaves the window.
            await asyncio.sleep(max(0.01, 60.0 - (now - self._stamps[0])))

    async def __aexit__(self, *_exc: object) -> None:
        self._semaphore.release()


class ArenaClient:
    """One arena connection.  Obtain instances through :data:`arena_clients`."""

    def __init__(
        self,
        *,
        connection_id: str,
        base_url: str,
        token: str | None,
        requests_per_minute: int = 60,
        max_concurrency: int = 1,
    ) -> None:
        self.connection_id = connection_id
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._limiter = _RateLimiter(requests_per_minute, max_concurrency)

    @property
    def token_configured(self) -> bool:
        return bool(self._token)

    def _headers(self) -> dict[str, str]:
        if not self._token:
            raise DomainError(
                "ARENA_TOKEN_MISSING",
                "This arena connection has no token configured.",
                status_code=422,
            )
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

    @staticmethod
    def _raise(response: httpx.Response) -> None:
        if response.is_success:
            return
        try:
            body = response.json()
        except ValueError:
            body = {}
        # The platform nests its error detail under "error" rather than the flat
        # {code, message} shape this backend uses.
        error = body.get("error") if isinstance(body.get("error"), dict) else {}
        message = (
            error.get("message")
            or error.get("key")
            or body.get("message")
            or body.get("detail")
            or response.reason_phrase
        )
        code = str(error.get("code") or body.get("code") or "ARENA_REQUEST_FAILED")
        # 429 and 5xx are worth retrying; 401/403 mean the credential or the
        # permission set is wrong and repeating the call will not change that.
        # 404 means the route does not exist -- also not retryable.
        retryable = response.status_code == 429 or response.status_code >= 500
        raise DomainError(
            code,
            str(message or "Arena request failed."),
            {"status_code": response.status_code},
            503 if response.status_code >= 500 else response.status_code,
            stage="ARENA",
            retryable=retryable,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: float = REQUEST_TIMEOUT,
        params: dict | None = None,
    ) -> dict:
        url = f"{self.base_url}{path}"
        async with self._limiter:
            try:
                # See ``Settings.respect_proxy_env``: the arena host is reached
                # directly, and inheriting the caller's egress proxy makes every
                # call fail at the connection layer once that proxy expires.
                async with httpx.AsyncClient(
                    timeout=timeout, trust_env=outbound_trust_env()
                ) as client:
                    response = await client.request(
                        method, url, headers=self._headers(), params=params
                    )
            except httpx.HTTPError as error:
                raise DomainError(
                    "ARENA_UNREACHABLE",
                    f"Arena endpoint is unreachable: {error}",
                    {"url": url},
                    503,
                    stage="ARENA",
                    retryable=True,
                ) from error
        self._raise(response)
        try:
            return response.json()
        except ValueError:
            return {}

    @staticmethod
    def _data(payload: object) -> object:
        """Return ``data`` from the platform's ``{data, success}`` envelope."""
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    async def probe(self) -> dict:
        """``whoami`` -- a cheap call that verifies both the token and the quota.

        The payload is the authoritative source for quotas and ``allowed_tools``,
        which is why the caller caches it rather than deriving either locally.
        """
        payload = await self._request("GET", PATHS["whoami"], timeout=PROBE_TIMEOUT)
        data = self._data(payload)
        return data if isinstance(data, dict) else {"raw": data}

    async def list_challenges(
        self,
        *,
        category: str | None = None,
        limit: int = MAX_PAGE_SIZE,
        offset: int = 0,
    ) -> dict:
        """Read the catalogue page.  Returns ``{"items": [...], "total": N}``.

        ``offset`` is sent for forward-compatibility but the platform ignores it,
        so callers must read ``total`` rather than assume they can page to the end.
        """
        # The platform clamps an over-large limit back to 10, so clamp it here and
        # keep the number that was actually honoured in the response.
        effective = max(1, min(limit, MAX_PAGE_SIZE))
        params: dict[str, object] = {"limit": effective}
        if category:
            params["category"] = category
        if offset:
            params["offset"] = offset
        payload = await self._request("GET", PATHS["challenges"], params=params)
        data = self._data(payload)
        if not isinstance(data, dict):
            return {"items": [], "total": 0, "limit_applied": effective}
        items = data.get("items")
        return {
            "items": [item for item in items if isinstance(item, dict)]
            if isinstance(items, list)
            else [],
            "total": data.get("total"),
            "limit_applied": effective,
        }

    @staticmethod
    def categories(items: list[dict]) -> dict[str, int]:
        """Count items per ``category`` label.

        The label is free-form -- some rows carry a chapter name instead of a
        category -- so this returns whatever was seen rather than intersecting
        against a fixed enum that would silently drop rows.
        """
        counts: dict[str, int] = {}
        for item in items:
            label = str(item.get("category") or "unknown")
            counts[label] = counts.get(label, 0) + 1
        return dict(sorted(counts.items()))


class _ArenaClientRegistry:
    """Cache of one client per connection, rebuilt when the row changes.

    Rebuilding on change matters: editing the token or the pace limits must take
    effect without a backend restart, but it must also replace the limiter rather
    than mutate a shared one mid-flight.
    """

    def __init__(self) -> None:
        self._clients: dict[str, tuple[object, ArenaClient]] = {}
        self._lock = asyncio.Lock()

    async def get(self, connection, token: str | None) -> ArenaClient:
        # Compared by identity of the mutable fields so a config edit is detected
        # without a version column on the table.
        fingerprint = (
            connection.base_url,
            connection.encrypted_token,
            connection.requests_per_minute,
            connection.max_concurrency,
            connection.enabled,
        )
        async with self._lock:
            cached = self._clients.get(connection.id)
            if cached and cached[0] == fingerprint:
                return cached[1]
            client = ArenaClient(
                connection_id=connection.id,
                base_url=connection.base_url,
                token=token,
                requests_per_minute=connection.requests_per_minute,
                max_concurrency=connection.max_concurrency,
            )
            self._clients[connection.id] = (fingerprint, client)
            return client

    def forget(self, connection_id: str) -> None:
        self._clients.pop(connection_id, None)

    def clear(self) -> None:
        self._clients.clear()


arena_clients = _ArenaClientRegistry()
