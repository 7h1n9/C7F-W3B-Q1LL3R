import asyncio
import json
import os
import shutil
import time
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import claude_proxy_endpoint, codex_proxy_endpoint
from app.core.database import get_session
from app.core.exceptions import DomainError
from app.core.llm_parameters import parameter_constraint
from app.engines.wire import (
    CHAT_COMPLETIONS,
    RESPONSES,
    normalize_wire_api,
    resolve_endpoint,
    responses_to_chat_body,
    strip_endpoint_path,
    to_responses_request,
)
from app.models.model_config import ModelConfig
from app.schemas.model_config import (
    CLI_PROVIDER_TYPES,
    ENDPOINT_CAPABLE_CLI_PROVIDERS,
    OPENAI_WIRE_PROVIDER_TYPES,
    ModelConfigUpdate,
    ModelConfigWrite,
    runtime_engine_type,
)
from app.services.crypto import decrypt_api_key, encrypt_api_key

router = APIRouter(prefix="/model-configs", tags=["settings"])

# The settings-page probe is an interactive check, not a full benchmark, so it
# keeps a wall-clock budget.  That budget must still exceed one real round trip:
# a reasoning model on a relay routinely needs several seconds before its first
# byte, and a budget shorter than that reports a false failure for a perfectly
# healthy endpoint.
MODEL_TEST_TOTAL_BUDGET_SECONDS = 60.0
MODEL_TEST_BASE_TIMEOUT_SECONDS = 15.0
MODEL_TEST_PROBE_TIMEOUT_SECONDS = 12.0
CODEX_CLI_PROBE_TIMEOUT_SECONDS = 3.0
CODEX_API_PROBE_TIMEOUT_SECONDS = 45.0
MODEL_TEST_CAPABILITY_CONCURRENCY = 3

# Vendored CLI engines that can back a model-config provider.  The identity here
# is the *base engine* name understood by ``muteki.solver.cli_driver``.
CLI_PROVIDER_ENGINES = {
    "codex_cli": "codex",
    "claude_cli": "claude",
    "cursor_cli": "cursor",
}
CLI_VERSION_PROBE_TIMEOUT_SECONDS = 8.0

# Human-facing names for the CLI providers, so probe messages read
# "Claude CLI executable not found" rather than the raw provider id.
CLI_PROVIDER_LABELS = {
    "codex_cli": "Codex",
    "claude_cli": "Claude",
    "cursor_cli": "Cursor",
}


def _stores_api_key(payload: ModelConfigWrite) -> bool:
    """Must this provider's secret be encrypted into the row?

    True for everything that authenticates with its own key: every OpenAI-wire
    provider (``encrypt_api_key`` rejects a blank value, so a key is effectively
    required there), plus a CLI provider pointed at an explicit endpoint instead
    of the host's own subscription login. Cursor's headless CLI has no endpoint
    flag, so it never is -- and it must stay False, or a Cursor config would be
    rejected for a key it has nowhere to put.
    """

    if payload.provider_type in OPENAI_WIRE_PROVIDER_TYPES:
        return True
    return payload.provider_type in ENDPOINT_CAPABLE_CLI_PROVIDERS and payload.base_url is not None


def _resolve_cli_driver(engine: str) -> tuple[object | None, str]:
    """Resolve a vendored CLI engine to ``(driver, bin_source)``.

    ``driver`` is the same object a live Worker would shell -- honouring the
    operator's ``MUTEKI_*_BIN`` override and the vendored known-good install
    list -- so the settings probe reports the executable a run would actually
    use instead of whatever happens to be first on PATH. It is ``None`` when the
    engine is unknown to the vendored registry. ``bin_source`` is one of the
    values documented on ``muteki.solver.cli_driver.resolve_engine_bin_source``;
    ``"fallback"`` means nothing runnable was found and the driver is holding a
    bare basename.
    """

    try:
        from muteki.solver.cli_driver import get_driver, resolve_engine_bin_source
    except Exception:  # noqa: BLE001 - a missing engine must not break settings
        return None, "unknown"
    try:
        driver = get_driver(engine)
    except (ValueError, OSError):
        return None, "unknown"
    try:
        source = str(resolve_engine_bin_source(engine) or "fallback")
    except Exception:  # noqa: BLE001
        source = "fallback"
    return driver, source


class _ProbeTimedOut(TimeoutError):
    pass


def _remaining_seconds(deadline: float) -> float:
    return max(0.0, deadline - time.perf_counter())


async def _post_probe(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict,
    deadline: float,
    configured_timeout: float,
) -> httpx.Response:
    remaining = _remaining_seconds(deadline)
    if remaining <= 0:
        raise _ProbeTimedOut("model test deadline exhausted")
    request_timeout = max(0.05, min(float(configured_timeout), remaining))
    try:
        return await asyncio.wait_for(
            client.post(url, headers=headers, json=payload, timeout=request_timeout),
            timeout=request_timeout,
        )
    except (TimeoutError, httpx.TimeoutException) as error:
        raise _ProbeTimedOut("model probe timed out") from error


async def _post_probe_negotiating(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict,
    deadline: float,
    configured_timeout: float,
) -> tuple[httpx.Response, dict[str, float | None]]:
    """Send one probe, retrying once when the upstream rejects a parameter.

    A relay may accept only a single ``temperature`` for a given model.  Without
    this the probe reports the endpoint as broken -- and a failed health probe
    is what empties the worker pool -- while the very same endpoint answers a
    body carrying the value it asked for.  The negotiated field/value pair is
    returned so the caller can surface it instead of swallowing it.
    """

    try:
        response = await _post_probe(
            client,
            url,
            headers=headers,
            payload=payload,
            deadline=deadline,
            configured_timeout=configured_timeout,
        )
        response.raise_for_status()
        return response, {}
    except httpx.HTTPStatusError as error:
        constraint = parameter_constraint(error)
        if constraint is None:
            raise
        field, allowed = constraint
        adjusted = dict(payload)
        if allowed is None:
            adjusted.pop(field, None)
        else:
            adjusted[field] = allowed
        response = await _post_probe(
            client,
            url,
            headers=headers,
            payload=adjusted,
            deadline=deadline,
            configured_timeout=configured_timeout,
        )
        response.raise_for_status()
        return response, {field: allowed}


async def _run_async_process(
    argv: list[str],
    *,
    timeout: float,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str, bool]:
    """Run a bounded subprocess and reap/kill it when the deadline expires."""

    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=max(0.05, timeout))
    except TimeoutError:
        await _terminate_process_tree(process)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=1.0)
        except (TimeoutError, ProcessLookupError):
            stdout, stderr = b"", b""
        return -1, stdout.decode("utf-8", errors="replace"), stderr.decode(
            "utf-8", errors="replace"
        ), True
    return (
        int(process.returncode if process.returncode is not None else -1),
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
        False,
    )


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    """Kill a timed-out probe and its children.

    On Windows the Codex npm shim launches a ``cmd``/Node wrapper which then
    launches the native Codex executable and a Node REPL. Killing only the
    wrapper leaves those children alive, so every timed-out settings probe can
    accumulate an orphaned Codex process tree.
    """

    pid = getattr(process, "pid", None)
    if os.name == "nt" and isinstance(pid, int):
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(killer.wait(), timeout=2.0)
            except TimeoutError:
                try:
                    killer.kill()
                except ProcessLookupError:
                    pass
        except (FileNotFoundError, OSError):
            pass
    try:
        process.kill()
    except ProcessLookupError:
        pass


async def _probe_openai_capabilities(
    client: httpx.AsyncClient,
    *,
    url: str,
    headers: dict[str, str],
    base_payload: dict,
    deadline: float,
    wire_api: str = CHAT_COMPLETIONS,
) -> list[dict]:
    """Probe the optional structured-output features of an OpenAI-wire provider.

    ``wire_api`` selects the dialect the payloads are rendered in.  A Responses
    provider rejects a Chat body outright, so an untranslated probe would report
    every capability as unsupported on an endpoint that is perfectly healthy.
    """

    def outbound(payload: dict) -> dict:
        if normalize_wire_api(wire_api) == RESPONSES:
            return to_responses_request(payload)
        return payload

    semaphore = asyncio.Semaphore(MODEL_TEST_CAPABILITY_CONCURRENCY)
    probes = (
        (
            "json_object",
            "structured",
            {**base_payload, "messages": [{"role": "user", "content": 'Return {"ok":true} as JSON.'}], "response_format": {"type": "json_object"}},
        ),
        (
            "json_schema",
            "structured",
            {
                **base_payload,
                "messages": [{"role": "user", "content": 'Return {"ok":true} as JSON.'}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "probe",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {"ok": {"type": "boolean"}},
                            "required": ["ok"],
                            "additionalProperties": False,
                        },
                    },
                },
            },
        ),
        (
            "agent_action",
            "action",
            {
                **base_payload,
                "messages": [{"role": "user", "content": json.dumps({"type": "finish", "result": "unsolved", "summary": "capability probe"})}],
                "response_format": {"type": "json_object"},
            },
        ),
        (
            "finish_action",
            "action",
            {
                **base_payload,
                "messages": [{"role": "user", "content": json.dumps({"type": "finish", "result": "unsolved", "summary": "capability probe"})}],
                "response_format": {"type": "json_object"},
            },
        ),
        (
            "tool_action",
            "action",
            {
                **base_payload,
                "messages": [{"role": "user", "content": json.dumps({"type": "tool", "phase": "INTAKE", "objective": "capability probe", "tool_name": "file_read", "arguments": {"path": "notes/probe.txt"}, "reason": "capability probe"})}],
                "response_format": {"type": "json_object"},
            },
        ),
        (
            "skill_action",
            "action",
            {
                **base_payload,
                "messages": [{"role": "user", "content": json.dumps({"type": "skill", "operation": "inspect", "phase": "INTAKE", "objective": "capability probe", "reason": "capability probe", "expected_use": "capability probe"})}],
                "response_format": {"type": "json_object"},
            },
        ),
    )

    async def run_probe(name: str, probe_kind: str, payload: dict) -> dict:
        async with semaphore:
            try:
                response, _ = await _post_probe_negotiating(
                    client,
                    url,
                    headers=headers,
                    payload=outbound(payload),
                    deadline=deadline,
                    configured_timeout=MODEL_TEST_PROBE_TIMEOUT_SECONDS,
                )
                return {"name": name, "ok": True, "status": "supported"}
            except _ProbeTimedOut:
                return {"name": name, "ok": False, "status": "timeout"}
            except httpx.HTTPStatusError as error:
                status_code = error.response.status_code
                return {
                    "name": name,
                    "ok": False,
                    "status": (
                        "rate_limited"
                        if status_code == 429
                        else "unsupported"
                        if probe_kind == "structured" and 400 <= status_code < 500
                        else "failed"
                    ),
                    "retry_after": error.response.headers.get("Retry-After"),
                }
            except httpx.HTTPError as error:
                return {
                    "name": name,
                    "ok": False,
                    "status": "unavailable",
                    "error": str(error)[:240],
                }

    return await asyncio.gather(
        *(run_probe(name, probe_kind, payload) for name, probe_kind, payload in probes)
    )


async def _stream_probe_codex_endpoint(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict,
    deadline: float,
    configured_timeout: float,
) -> tuple[bool, str]:
    """POST a streaming Responses probe; return once the first event arrives.

    The relay hosting this endpoint answers a streaming Responses request in
    seconds but leaves the *non-streaming* form of the identical request hanging
    until the client gives up (measured on api.gavinchen.top: same key, model and
    payload, ``stream: true`` returned in 8s while the buffered form never
    returned).  Codex itself always streams, so the only faithful reachability
    test is a streaming one -- anything else reports a false failure for an
    endpoint Codex drives without trouble.
    """
    remaining = _remaining_seconds(deadline)
    if remaining <= 0:
        raise _ProbeTimedOut("model test deadline exhausted")
    request_timeout = max(0.05, min(float(configured_timeout), remaining))

    async def run() -> tuple[bool, str]:
        async with client.stream(
            "POST", url, headers=headers, json=payload, timeout=request_timeout
        ) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", "replace")
                return False, f"returned HTTP {response.status_code}: {body}"
            # Read the stream whole rather than iterating its lines: httpx's
            # async ``aiter_lines()`` never yields against this relay's SSE
            # framing (measured: ``aiter_lines`` hung for the full timeout while
            # ``aread`` returned the same 5.4 KB body in 8.6s).  The probe wants
            # a completed round trip anyway, so buffering it is also the more
            # faithful test.
            body = await response.aread()
        if not body.strip():
            return False, "returned an empty event stream"
        return True, ""

    try:
        return await asyncio.wait_for(run(), timeout=request_timeout)
    except (TimeoutError, httpx.TimeoutException) as error:
        raise _ProbeTimedOut("model probe timed out") from error


async def _probe_codex_endpoint(
    *,
    base_url: str,
    api_key: str,
    model: str,
    wire_api: str,
    deadline: float,
) -> tuple[bool, str, bool]:
    remaining = _remaining_seconds(deadline)
    if remaining <= 0:
        return False, "Codex endpoint probe skipped: deadline exhausted", True
    timeout = min(CODEX_API_PROBE_TIMEOUT_SECONDS, remaining)
    # Codex CLI speaks the Responses wire protocol and nothing else: `"chat"` was
    # removed upstream and `"chat_completions"` never deserialised (codex aborts
    # with ``unknown variant `chat_completions``).  Probing /chat/completions
    # would report a reachability the CLI itself can never reproduce, so the
    # probe is pinned here and ``wire_api`` is accepted only for call-site
    # compatibility.
    url = f"{base_url.rstrip('/')}/responses"
    # The output budget must clear the relay's floor for reasoning models: a
    # Responses request asking for 16-64 output tokens makes some relays hang
    # until the client times out (measured against api.gavinchen.top, where 16
    # and 64 never returned and 256/512 answered).  A probe that lies about a
    # working endpoint is worse than a slightly slower probe.
    payload = {
        "model": model,
        "input": "Reply with exactly: OK",
        "max_output_tokens": 512,
        "store": False,
        "stream": True,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "text/event-stream",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            ok, detail = await _stream_probe_codex_endpoint(
                client,
                url,
                headers=headers,
                payload=payload,
                deadline=deadline,
                configured_timeout=timeout,
            )
    except _ProbeTimedOut:
        return False, f"Codex endpoint probe timed out (>{timeout:g}s)", True
    except (httpx.HTTPError, OSError, ValueError) as error:
        return False, f"Codex endpoint unavailable: {str(error)[:240]}", False
    if not ok:
        return False, f"Codex endpoint {detail.strip()[:240]}", False
    return True, "", False


async def _run_codex_health_probe(
    profile: dict[str, object],
    env: dict[str, str],
    timeout: float,
) -> tuple[bool, str, bool, bool, str | None]:
    if timeout <= 0:
        return (
            False,
            "Codex API health probe skipped: deadline exhausted",
            True,
            False,
            "CODEX_API_PROBE_TIMEOUT",
        )
    base_url = str(profile.get("base_url") or "").strip()
    api_key = str(env.get("OPENAI_API_KEY") or "").strip()
    model = str(profile.get("model") or "").strip()
    wire_api = str(profile.get("wire_api") or "responses").strip() or "responses"
    if not base_url:
        return (
            False,
            "Codex endpoint base URL is missing",
            False,
            False,
            "CODEX_CLI_ENDPOINT_UNAVAILABLE",
        )
    if not api_key:
        return (
            False,
            "Codex endpoint API key is missing",
            False,
            False,
            "CODEX_CLI_ENDPOINT_UNAVAILABLE",
        )

    executable = os.environ.get("MUTEKI_CODEX_BIN", "").strip() or shutil.which("codex")
    api_timeout = min(timeout, CODEX_API_PROBE_TIMEOUT_SECONDS)
    cli_timeout = min(timeout, CODEX_CLI_PROBE_TIMEOUT_SECONDS)

    async def run_cli_version() -> tuple[int, str, str, bool]:
        if not executable:
            return -1, "", "codex executable not found", False
        try:
            return await _run_async_process(
                [executable, "--version"],
                timeout=cli_timeout,
                env=env,
            )
        except (FileNotFoundError, OSError) as error:
            return -1, "", str(error)[:240], False

    probe_deadline = time.perf_counter() + api_timeout
    try:
        endpoint_result, cli_result = await asyncio.gather(
            _probe_codex_endpoint(
                base_url=base_url,
                api_key=api_key,
                model=model,
                wire_api=wire_api,
                deadline=probe_deadline,
            ),
            run_cli_version(),
        )
    except Exception as error:  # noqa: BLE001
        return (
            False,
            str(error)[:240],
            False,
            False,
            "CODEX_CLI_ENDPOINT_UNAVAILABLE",
        )

    endpoint_ok, endpoint_detail, endpoint_timed_out = endpoint_result
    returncode, stdout, stderr, cli_timed_out = cli_result
    cli_available = returncode == 0 and not cli_timed_out
    if endpoint_timed_out:
        return False, endpoint_detail, True, cli_available, "CODEX_API_PROBE_TIMEOUT"
    if cli_timed_out:
        return (
            False,
            f"Codex CLI version probe timed out (>{cli_timeout:g}s)",
            True,
            False,
            "CODEX_CLI_PROBE_TIMEOUT",
        )
    if not executable:
        return (
            False,
            "Codex CLI executable not found",
            False,
            False,
            "CODEX_CLI_UNAVAILABLE",
        )
    if not cli_available:
        version_detail = (stderr or stdout or "").strip().splitlines()
        detail = f"Codex CLI version probe exited {returncode}"
        if version_detail:
            detail += f": {version_detail[-1][:240]}"
        return False, detail, False, False, "CODEX_CLI_UNAVAILABLE"
    if not endpoint_ok:
        return (
            False,
            endpoint_detail,
            False,
            True,
            "CODEX_CLI_ENDPOINT_UNAVAILABLE",
        )
    return True, "", False, True, None


def _anthropic_messages_url(base_url: str) -> str:
    """Append the Anthropic Messages path exactly once.

    An operator naturally pastes the full endpoint they were given, and this
    Anthropic-compatible relay's docs literally publish ``.../v1/messages`` as
    the base URL.  Appending unconditionally produced
    ``.../v1/messages/v1/messages`` -> HTTP 404 "Unknown path", which reads as
    a broken config rather than a double suffix.  Accept either form.

    The OpenAI-wire spelling matters just as much.  A relay that serves both
    ``/v1/messages`` and ``/v1/chat/completions`` is common, and an operator who
    pasted the *chat* endpoint (``.../v1/chat/completions``) would get
    ``.../v1/chat/completions/v1/messages`` -> 404 -- a URL no relay serves.  So
    every known endpoint suffix is stripped before appending, the same helper
    :func:`resolve_endpoint` uses for the OpenAI wire: one definition of "what
    counts as a pasted endpoint" for both directions.
    """

    base = strip_endpoint_path(base_url)
    if base.endswith("/v1"):
        return f"{base}/messages"
    return f"{base}/v1/messages"


async def _probe_cursor_endpoint(
    *,
    base_url: str,
    api_key: str,
    deadline: float,
) -> tuple[bool, str, bool]:
    """Reachability probe for a Cursor CLI's pinned OpenAI-compatible gateway.

    Deliberately shallow: Cursor's headless CLI talks to its own gateway over an
    OpenAI-style API reached via `--endpoint <url>`, and the exact model ids and
    payload shape are Cursor's business, not ours.  Sending a fabricated
    completion here would fail for reasons that say nothing about whether the
    operator's endpoint is usable, so this only answers "is it there and does
    its auth work".

    A 401/403 is surfaced as a failure because the schema requires an api_key
    with an endpoint -- a rejected key is a real misconfiguration.  A 404/405
    means the host answered, which is all a reachability check needs.
    """

    remaining = _remaining_seconds(deadline)
    if remaining <= 0:
        return False, "Cursor endpoint probe skipped: deadline exhausted", True
    timeout = min(MODEL_TEST_PROBE_TIMEOUT_SECONDS, remaining)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            # GET, not POST: a reachability check must not create work on the
            # operator's gateway (or bill a completion) as a side effect.
            response = await client.get(base_url.rstrip("/") + "/models", headers=headers)
    except httpx.TimeoutException:
        return False, f"Cursor endpoint probe timed out (>{timeout:g}s)", True
    except (httpx.HTTPError, OSError, ValueError) as error:
        return False, f"Cursor endpoint unavailable: {str(error)[:240]}", False

    if response.status_code in (401, 403):
        body = str(getattr(response, "text", "") or "").strip().replace("\n", " ")[:240]
        return False, f"Cursor endpoint rejected the API key (HTTP {response.status_code})" + (
            f": {body}" if body else ""
        ), False
    return True, "", False


async def _probe_claude_endpoint(
    *,
    base_url: str,
    api_key: str,
    model: str,
    deadline: float,
) -> tuple[bool, str, bool]:
    """One direct HTTP probe of a Claude CLI's pinned Anthropic endpoint.

    Mirrors ``EndpointDriver.health_detail`` in ``muteki.solver.cli_driver`` --
    same URL, same auth header pair, same minimal body
    -- but as an async, deadline-aware request so it composes with the settings
    page's budget instead of shelling out to curl and blocking the event loop.
    """

    remaining = _remaining_seconds(deadline)
    if remaining <= 0:
        return False, "Claude endpoint probe skipped: deadline exhausted", True
    timeout = min(MODEL_TEST_PROBE_TIMEOUT_SECONDS, remaining)
    if not api_key:
        return False, "Claude endpoint API key is missing", False
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model or "claude-3-5-haiku-latest",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "OK"}],
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await _post_probe(
                client,
                _anthropic_messages_url(base_url),
                headers=headers,
                payload=payload,
                deadline=deadline,
                configured_timeout=timeout,
            )
            response.raise_for_status()
    except _ProbeTimedOut:
        return False, f"Claude endpoint probe timed out (>{timeout:g}s)", True
    except httpx.HTTPStatusError as error:
        status_code = error.response.status_code
        body = str(getattr(error.response, "text", "") or "").strip().replace("\n", " ")[:240]
        detail = f"Claude endpoint returned HTTP {status_code}"
        if body:
            detail += f": {body}"
        return False, detail, False
    except (httpx.HTTPError, OSError, ValueError) as error:
        return False, f"Claude endpoint unavailable: {str(error)[:240]}", False
    return True, "", False


async def _probe_claude_proxy_endpoint(
    *,
    capabilities: dict[str, object],
    model: str,
    deadline: float,
) -> tuple[bool, str, bool] | None:
    """Probe the local protocol-translating proxy, when this config routes via it.

    Why the stored ``base_url`` is the wrong thing to probe for such a config
    -------------------------------------------------------------------------
    ``route_via_proxy`` means the CLI's traffic goes through the local shim at
    ``127.0.0.1:<random port>``, which speaks Anthropic Messages and translates to
    OpenAI for whatever upstream the config names. The stored ``base_url`` is that
    upstream -- a **chat-completions** relay. Probing it with an Anthropic
    Messages body asks an OpenAI endpoint for ``/v1/messages`` and gets a 404,
    every time, by construction.

    The consequence was not a cosmetic red light: the probe's result is written
    into ``capabilities_json``, and the failure path is where ``route_via_proxy``
    and ``model_aliases`` were being lost -- so a config that works perfectly at
    solve time reported ``CLAUDE_CLI_ENDPOINT_UNAVAILABLE`` in Settings and, on
    the next probe, quietly stopped routing through the proxy at all.

    Returns ``None`` when this config does not route via the proxy, so the caller
    falls through to the direct probe. Returns a failure only when the config asks
    for the proxy and the proxy is genuinely not answering -- the one condition
    that really does break the solve.
    """

    if not capabilities.get("route_via_proxy"):
        return None
    state = claude_proxy_endpoint()
    if not state:
        return (
            False,
            "route_via_proxy is set but the local Claude proxy is not running; "
            "every Worker turn would talk to the upstream relay directly and be refused",
            False,
        )
    remaining = _remaining_seconds(deadline)
    if remaining <= 0:
        return False, "Claude proxy probe skipped: deadline exhausted", True
    timeout = min(MODEL_TEST_PROBE_TIMEOUT_SECONDS, remaining)
    # The proxy has no key of its own: it presents this token, and its own
    # ``model_aliases`` translate whatever the CLI named into the upstream model.
    token = str(state.get("token") or "")
    base = str(state.get("host_base_url") or "").rstrip("/")
    if not base:
        return False, "the local Claude proxy recorded no reachable host URL", False
    headers = {
        "x-api-key": token,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model or "claude-sonnet-5",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "OK"}],
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await _post_probe(
                client,
                f"{base}/v1/messages",
                headers=headers,
                payload=payload,
                deadline=deadline,
                configured_timeout=timeout,
            )
    except Exception as error:  # noqa: BLE001 - a failed probe is a report, not a crash
        return False, f"local Claude proxy probe failed: {type(error).__name__}", False
    if response is None:
        return False, "local Claude proxy probe returned no response", True
    if response.is_success:
        return True, "local Claude proxy answered", False
    return (
        False,
        f"local Claude proxy answered HTTP {response.status_code}: "
        f"{(response.text or '')[:160]}",
        False,
    )


async def _test_cli_provider_config(
    item: ModelConfig,
    session: AsyncSession,
    *,
    capabilities: dict[str, object],
    probe_status: dict[str, str],
    started: float,
    deadline: float,
) -> dict:
    """Settings probe for a plain CLI provider (``claude_cli`` / ``cursor_cli``).

    Deliberately cheaper than the vendored ``health_detail`` hello turn (up to
    120s x 3 attempts): the settings page is an interactive check with a ~12s
    budget, so this runs the driver's own ``version_argv`` and, when the provider
    pins an endpoint, one direct probe of that endpoint. The endpoint is probed
    because a version-only check would report green while the endpoint the
    operator configured is unreachable -- a false pass that costs a whole run.

    ``codex_cli`` keeps its own branch: it is the only engine whose custom
    endpoint speaks the OpenAI Responses wire rather than Anthropic Messages.
    """

    provider = item.provider_type
    engine = CLI_PROVIDER_ENGINES[provider]
    label = CLI_PROVIDER_LABELS.get(provider, provider)
    # "codex_cli" -> "CODEX_CLI", so the codes below read CODEX_CLI_UNAVAILABLE,
    # CLAUDE_CLI_PROBE_TIMEOUT, CURSOR_CLI_ENDPOINT_UNAVAILABLE.
    code_prefix = provider.upper()
    driver, bin_source = _resolve_cli_driver(engine)

    version = ""
    available = False
    timed_out = False
    failures: list[str] = []

    if driver is None:
        failures.append(f"{label} CLI engine is not available in this build")
    else:
        argv = [str(part) for part in driver.version_argv()]
        remaining = _remaining_seconds(deadline)
        if remaining <= 0:
            timed_out = True
            failures.append(f"{label} CLI version probe skipped: deadline exhausted")
        else:
            probe_timeout = min(CLI_VERSION_PROBE_TIMEOUT_SECONDS, remaining)
            try:
                returncode, stdout, stderr, timed_out = await _run_async_process(
                    argv, timeout=probe_timeout
                )
            except (FileNotFoundError, OSError) as error:
                failures.append(f"{label} CLI executable not runnable: {type(error).__name__}")
            else:
                version = (stdout or stderr or "").strip()[-300:]
                available = returncode == 0 and not timed_out
                if timed_out:
                    failures.append(f"{label} CLI version probe timed out (>{probe_timeout:g}s)")
                elif returncode != 0:
                    failures.append(version or f"{engine} exited with {returncode}")
        if bin_source == "fallback" and not available and not timed_out:
            # The driver is holding a bare basename because nothing runnable was
            # found anywhere. Say so explicitly instead of leaving the operator
            # with a bare FileNotFoundError and no remedy.
            failures.append(
                f"no runnable {label} CLI found; set MUTEKI_{engine.upper()}_BIN to pin one"
            )

    probe_status[f"{provider}_version"] = (
        "timeout" if timed_out else "supported" if available else "unavailable"
    )

    # A pinned endpoint is available for every CLI provider now.  The probe is
    # protocol-aware: claude_cli speaks Anthropic Messages, while cursor_cli's
    # endpoint is an OpenAI-compatible gateway consumed via `--endpoint`, so a
    # Messages-shaped body would be meaningless there and only reachability is
    # checked.
    endpoint_required = provider in ENDPOINT_CAPABLE_CLI_PROVIDERS and bool(item.base_url)
    endpoint_ok: bool | None = None
    # Hoisted: the completion message below reports it, and that message is built
    # whether or not this provider pinned an endpoint.
    proxy_note = ""
    if endpoint_required:
        api_key = decrypt_api_key(item.encrypted_api_key) if item.encrypted_api_key else ""
        # A config that routes through the local proxy must be probed *there*: its
        # stored base_url is the OpenAI-wire upstream the proxy translates for, so
        # an Anthropic Messages probe against it can only ever 404.  Routing the
        # check to the proxy also means "green" now actually predicts a working
        # solve, which it did not before.
        proxy_result = (
            await _probe_claude_proxy_endpoint(
                capabilities=capabilities,
                model=str(item.model_name or ""),
                deadline=deadline,
            )
            if provider == "claude_cli"
            else None
        )
        if proxy_result is not None:
            endpoint_ok, detail, endpoint_timed_out = proxy_result
            proxy_note = " (via local proxy)"
            probe_status[f"{provider}_proxy"] = "supported" if endpoint_ok else "failed"
        elif provider == "cursor_cli":
            endpoint_ok, detail, endpoint_timed_out = await _probe_cursor_endpoint(
                base_url=str(item.base_url),
                api_key=api_key,
                deadline=deadline,
            )
        else:
            endpoint_ok, detail, endpoint_timed_out = await _probe_claude_endpoint(
                base_url=str(item.base_url),
                api_key=api_key,
                model=str(item.model_name or ""),
                deadline=deadline,
            )
        timed_out = timed_out or endpoint_timed_out
        probe_status[f"{provider}_endpoint"] = (
            "timeout" if endpoint_timed_out else "supported" if endpoint_ok else "failed"
        )
        if not endpoint_ok:
            failures.append(detail)

    ok = available and endpoint_ok is not False
    if ok:
        error_code = None
    elif timed_out:
        error_code = f"{code_prefix}_PROBE_TIMEOUT"
    elif endpoint_ok is False:
        error_code = f"{code_prefix}_ENDPOINT_UNAVAILABLE"
    else:
        error_code = f"{code_prefix}_UNAVAILABLE"

    capabilities.update(
        {
            "reachable": ok,
            "normal_chat": ok,
            "cli_available": available,
            "cli_executable": str(getattr(driver, "bin", "") or "") if driver is not None else "",
            "cli_bin_source": bin_source,
            "cli_version": version,
            "model": str(item.model_name or ""),
            "endpoint_required": endpoint_required,
            "endpoint_reachable": endpoint_ok,
            "error_code": error_code,
            "error": "; ".join(failures)[:400],
            "latency": round((time.perf_counter() - started) * 1000),
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "recommended_protocol": provider,
            "timed_out": timed_out,
            "probe_status": dict(probe_status),
        }
    )
    item.capabilities_json = capabilities
    # A CLI agent answers with its own tool loop, not with a JSON envelope we
    # constrain, so the structured-output knobs are meaningless for it.
    item.supports_json_schema = False
    item.supports_json_object = False
    item.supports_native_tool_call = True
    item.last_test_at = datetime.now(UTC)
    item.last_test_ok = ok
    await session.commit()

    message = (
        f"{label} CLI 与端点测试完成{proxy_note}"
        if ok and endpoint_required
        else f"{label} CLI 能力测试完成"
        if ok
        else f"{label} 测试超时（未在预算内完成）"
        if timed_out
        else f"{label} 测试失败"
    )
    return {
        "data": {
            "ok": ok,
            "message": message,
            "capabilities": capabilities,
            "elapsed_ms": capabilities["latency_ms"],
            "timed_out": timed_out,
            "incomplete_probe_count": sum(
                status == "timeout" for status in probe_status.values()
            ),
            "probe_status": probe_status,
        }
    }


def _surface_wire_api(item: ModelConfig) -> str | None:
    """The dialect the settings page should show for this configuration.

    Codex is pinned to Responses whenever it has an endpoint, an OpenAI-wire
    provider surfaces its stored choice (normalising a legacy NULL to the
    Chat Completions default), and a CLI provider has no dialect at all --
    it owns its own transport.
    """

    if item.provider_type == "codex_cli":
        return "responses" if item.base_url else None
    if item.provider_type in OPENAI_WIRE_PROVIDER_TYPES:
        # getattr: this is also called with lightweight test doubles that
        # predate the column.
        return normalize_wire_api(getattr(item, "wire_api", None), default=CHAT_COMPLETIONS)
    return None


def read(item: ModelConfig) -> dict:
    capabilities = item.capabilities_json or {}
    roles = capabilities.get("roles") if isinstance(capabilities, dict) else None
    if not isinstance(roles, list) or not roles:
        roles = ["worker"]
    return {
        "id": item.id,
        "name": item.name,
        "provider_type": item.provider_type,
        # The Worker engine this provider produces at run time. For every
        # provider but local_proxy it mirrors the type; local_proxy maps onto
        # openai_compatible. Surfacing it here keeps the frontend's run-time
        # engine labels in sync with the backend vocabulary instead of
        # re-deriving the mapping in TypeScript.
        "runtime_engine_type": runtime_engine_type(item.provider_type),
        "base_url": item.base_url,
        "wire_api": _surface_wire_api(item),
        "model_name": item.model_name,
        "reasoning_effort": (
            capabilities.get("reasoning_effort", "medium")
            if item.provider_type == "codex_cli"
            else None
        ),
        "enabled": item.enabled,
        "api_key_configured": bool(item.encrypted_api_key),
        "action_protocol": item.action_protocol,
        "structured_output_mode": item.structured_output_mode,
        "supports_json_schema": item.supports_json_schema,
        "supports_json_object": item.supports_json_object,
        "supports_native_tool_call": item.supports_native_tool_call,
        "request_timeout_seconds": item.request_timeout_seconds,
        "max_output_tokens": item.max_output_tokens,
        "temperature": item.temperature,
        "max_retries": item.max_retries,
        "retry_base_seconds": item.retry_base_seconds,
        "rate_limit_cooldown_seconds": item.rate_limit_cooldown_seconds,
        "requests_per_minute": item.requests_per_minute,
        "max_concurrency": item.max_concurrency,
        "context_token_limit": item.context_token_limit,
        "last_test_at": item.last_test_at.isoformat() if item.last_test_at else None,
        "last_test_ok": item.last_test_ok,
        # Surfaced as first-class fields, not buried in ``capabilities``: they
        # are operator-set routing decisions, and the settings page has to be
        # able to read and write them without a hand-written SQL patch.
        "route_via_proxy": bool(capabilities.get("route_via_proxy")),
        "model_aliases": (
            capabilities.get("model_aliases")
            if isinstance(capabilities.get("model_aliases"), dict)
            else {}
        ),
        "capabilities": capabilities,
        "roles": [str(role) for role in roles if str(role) in {"worker", "coordinator_reason"}],
    }


@router.get("")
async def list_model_configs(session: AsyncSession = Depends(get_session)) -> dict:
    items = list((await session.scalars(select(ModelConfig).order_by(ModelConfig.name))).all())
    return {"data": [read(item) for item in items]}


#: Keys that live in ``capabilities_json`` but are operator-set routing
#: decisions rather than probe results.  Kept in one place so create and update
#: apply them identically and the ``model_dump`` exclusions cannot drift.
_ROUTING_CAPABILITY_KEYS = ("route_via_proxy", "model_aliases")


def _apply_routing_capabilities(
    capabilities: dict, payload: ModelConfigWrite
) -> dict:
    """Fold the routing settings into a config's ``capabilities_json``.

    ``None`` means "leave what is stored" for both keys, so a partial update --
    or a payload built by a client that does not know about them -- cannot
    silently clear a working route.  An empty ``model_aliases`` map is a real
    instruction (clear the aliases) and is persisted as such.
    """

    if payload.route_via_proxy is not None:
        capabilities["route_via_proxy"] = bool(payload.route_via_proxy)
    if payload.model_aliases is not None:
        aliases = {
            str(name).strip(): str(model).strip()
            for name, model in payload.model_aliases.items()
            if str(name).strip() and str(model).strip()
        }
        if aliases:
            capabilities["model_aliases"] = aliases
        else:
            capabilities.pop("model_aliases", None)
    return capabilities


@router.post("", status_code=201)
async def create_model_config(
    payload: ModelConfigWrite, session: AsyncSession = Depends(get_session)
) -> dict:
    capabilities = _apply_routing_capabilities({"roles": list(payload.roles)}, payload)
    if payload.provider_type == "codex_cli":
        capabilities["reasoning_effort"] = payload.reasoning_effort or "medium"
        if payload.base_url:
            # Codex accepts only the Responses wire protocol (see
            # _probe_codex_endpoint); never persist a value it cannot load.
            capabilities["wire_api"] = "responses"
    item = ModelConfig(
        name=payload.name,
        provider_type=payload.provider_type,
        base_url=str(payload.base_url).rstrip("/") if payload.base_url else None,
        # The schema validator has already normalised this: pinned to
        # "responses" for Codex, defaulted for OpenAI-wire providers, and None
        # for a CLI provider that owns its own transport.
        wire_api=payload.wire_api,
        model_name=payload.model_name,
        encrypted_api_key=encrypt_api_key(payload.api_key or "") if _stores_api_key(payload) else None,
        enabled=payload.enabled,
        capabilities_json=capabilities,
        **payload.model_dump(
            exclude={
                "name", "provider_type", "base_url", "wire_api", "model_name",
                "api_key", "enabled", "roles", "reasoning_effort",
                *_ROUTING_CAPABILITY_KEYS,
            }
        ),
    )
    session.add(item)
    await session.commit()
    return {"data": read(item)}


@router.put("/{config_id}")
async def update_model_config(
    config_id: str, payload: ModelConfigUpdate, session: AsyncSession = Depends(get_session)
) -> dict:
    item = await session.get(ModelConfig, config_id)
    if item is None:
        raise DomainError(
            "MODEL_CONFIG_NOT_FOUND", "Model configuration not found.", status_code=404
        )
    previous_provider_type = item.provider_type
    item.name, item.provider_type, item.base_url, item.model_name, item.enabled = (
        payload.name,
        payload.provider_type,
        str(payload.base_url).rstrip("/") if payload.base_url else None,
        payload.model_name,
        payload.enabled,
    )
    # Already normalised by the schema validator (see create_model_config).
    item.wire_api = payload.wire_api
    capabilities = dict(item.capabilities_json or {})
    capabilities["roles"] = list(payload.roles)
    if payload.provider_type == "codex_cli":
        capabilities["reasoning_effort"] = payload.reasoning_effort or "medium"
        if payload.base_url:
            # Codex accepts only the Responses wire protocol; a legacy
            # `chat_completions` value must be rewritten, not preserved.
            capabilities["wire_api"] = "responses"
        else:
            capabilities.pop("wire_api", None)
    else:
        capabilities.pop("reasoning_effort", None)
        capabilities.pop("wire_api", None)
    _apply_routing_capabilities(capabilities, payload)
    item.capabilities_json = capabilities
    for key, value in payload.model_dump(
        exclude={
            "name", "provider_type", "base_url", "wire_api", "model_name",
            "api_key", "enabled", "roles", "reasoning_effort",
            *_ROUTING_CAPABILITY_KEYS,
        }
    ).items():
        setattr(item, key, value)
    if not _stores_api_key(payload):
        # Cursor's headless CLI has no endpoint flag, and a Codex/Claude CLI
        # switched back to the host's own subscription login no longer has an
        # endpoint for the stored secret to belong to.
        item.encrypted_api_key = None
    elif payload.api_key:
        item.encrypted_api_key = encrypt_api_key(payload.api_key)
    elif payload.provider_type in CLI_PROVIDER_TYPES and payload.provider_type != previous_provider_type:
        # Provider switched without a replacement secret: never carry the
        # previous provider's credential across an endpoint contract change.
        item.encrypted_api_key = None
    await session.commit()
    return {"data": read(item)}


def _reference_counts_query(config_id: str):
    """One count per table that points at ``model_configs.id``.

    The table list mirrors the foreign keys on ``model_configs``; a count is
    built from ORM entities so no identifier reaches SQL text from user input.
    ``blocking`` (below) is what the UI warns about, ``references`` is context.
    """

    from sqlalchemy import func, select

    from app.models.conversation import ChallengeConversation
    from app.models.run import AgentTurn, RunAttempt, SolveRun
    from app.models.skill import ModelSkillBinding

    models = {
        "solve_runs": SolveRun,
        "run_attempts": RunAttempt,
        "agent_turns": AgentTurn,
        "challenge_conversations": ChallengeConversation,
        "model_skill_bindings": ModelSkillBinding,
    }
    return {
        label: select(func.count()).select_from(model).where(model.model_config_id == config_id)
        for label, model in models.items()
    }


async def _model_config_reference_counts(session: AsyncSession, config_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for label, statement in _reference_counts_query(config_id).items():
        counts[label] = int((await session.scalar(statement)) or 0)
    return counts


@router.get("/{config_id}/usage")
async def model_config_usage(config_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    """Report what still points at this configuration, so a delete is explainable.

    Every reference is currently detachable -- history is snapshotted and the
    rest is SET NULL -- so ``delete_safe`` is True.  It stays an explicit field
    so a future RESTRICT key surfaces here instead of as a blanket 500.
    """

    item = await session.get(ModelConfig, config_id)
    if item is None:
        raise DomainError("MODEL_CONFIG_NOT_FOUND", "Model configuration not found.", status_code=404)

    references = await _model_config_reference_counts(session, config_id)
    return {
        "data": {
            "id": config_id,
            "name": item.name,
            "provider_type": item.provider_type,
            "delete_safe": True,
            "blocking": [],
            "references": references,
        }
    }


@router.delete("/{config_id}", status_code=204)
async def delete_model_config(config_id: str, session: AsyncSession = Depends(get_session)) -> None:
    item = await session.get(ModelConfig, config_id)
    if item is None:
        raise DomainError("MODEL_CONFIG_NOT_FOUND", "Model configuration not found.", status_code=404)

    try:
        await session.delete(item)
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        # A RESTRICT foreign key is the only way this fails today, and it is a
        # caller-facing conflict, not a backend outage -- without this the raw
        # driver error escaped as a blanket 500 "Backend request failed.".
        references = await _model_config_reference_counts(session, config_id)
        blocking = sorted(name for name, count in references.items() if count)
        raise DomainError(
            "MODEL_CONFIG_IN_USE",
            "该模型配置仍被其他数据引用，无法删除："
            + "、".join(f"{name} × {references[name]}" for name in blocking),
            status_code=409,
            details={"references": references, "blocking": blocking},
        ) from error


@router.post("/{config_id}/test")
async def test_model_config(config_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    item = await session.get(ModelConfig, config_id)
    if item is None:
        raise DomainError(
            "MODEL_CONFIG_NOT_FOUND", "Model configuration not found.", status_code=404
        )

    started = time.perf_counter()
    deadline = started + MODEL_TEST_TOTAL_BUDGET_SECONDS
    timed_out = False
    probe_status: dict[str, str] = {}
    configured_roles = read(item)["roles"]
    # Seed from the stored capabilities so a probe only rewrites the fields it
    # actually measures.  Building a fresh dict from scratch silently dropped
    # every key this function does not know about -- including hand-set ones like
    # ``route_via_proxy``, which is what routes a Claude CLI through the local
    # protocol-translating proxy.  Losing it is not a visible failure: the config
    # keeps working until the next solve, which then talks to the upstream relay
    # directly, gets its request refused, and reports an unexplained
    # ``NO_HEALTHY_WORKER_PROFILE``.
    capabilities: dict[str, object] = dict(item.capabilities_json or {})
    capabilities.update(
        {
            "reachable": False,
            "normal_chat": False,
            "json_object": False,
            "json_schema": False,
            "native_tool_call": False,
            "agent_action": False,
            "tool_action": False,
            "finish_action": False,
            "recommended_protocol": item.action_protocol,
            "skill_action": False,
            "max_output_tokens": item.max_output_tokens,
            "latency": None,
            "usage": {},
            "quota_state": "unknown",
            "rate_limit_state": "unknown",
            "retry_after": None,
            "roles": configured_roles,
        }
    )
    if item.provider_type in ("claude_cli", "cursor_cli"):
        return await _test_cli_provider_config(
            item,
            session,
            capabilities=capabilities,
            probe_status=probe_status,
            started=started,
            deadline=deadline,
        )
    if item.provider_type == "codex_cli":
        capabilities["model"] = str(item.model_name or "")
        capabilities["reasoning_effort"] = (item.capabilities_json or {}).get("reasoning_effort", "medium")
        if item.base_url:
            api_key = decrypt_api_key(item.encrypted_api_key) if item.encrypted_api_key else ""
            # ``route_via_proxy`` means the Worker's traffic goes through the
            # local Responses proxy, whose port is random, not the stored
            # ``base_url``. Probing the stored value would ask a chat-only relay
            # for /responses and get a 404 by construction -- the same false-red
            # trap the Claude side documented, and here it is the more dangerous
            # one: it would report the exact config the proxy exists to fix as
            # broken.
            probe_base_url = item.base_url
            probe_api_key = api_key
            # ``capabilities`` was seeded from the stored row above, so this
            # reads the flag the operator actually set.
            if capabilities.get("route_via_proxy"):
                codex_proxy_state = codex_proxy_endpoint()
                if not codex_proxy_state:
                    probe_status["codex_proxy"] = "unavailable"
                    capabilities.update(
                        {
                            "reachable": False,
                            "normal_chat": False,
                            "error_code": "CODEX_CLI_ENDPOINT_UNAVAILABLE",
                            "error": (
                                "route_via_proxy is set but the local Codex proxy is not "
                                "running; every Worker turn would ask the upstream relay for "
                                "/responses directly and be refused"
                            ),
                            "probe_status": dict(probe_status),
                            "recommended_protocol": "codex_cli",
                            "route_via_proxy": True,
                        }
                    )
                    item.capabilities_json = capabilities
                    item.last_test_at = datetime.now(UTC)
                    item.last_test_ok = False
                    await session.commit()
                    return {
                        "data": {
                            "ok": False,
                            "message": (
                                "Codex API 能力测试失败："
                                "已开启 route_via_proxy 但本地 Codex 代理未运行"
                            ),
                            "capabilities": capabilities,
                            "elapsed_ms": capabilities["latency_ms"],
                            "timed_out": False,
                            "incomplete_probe_count": 0,
                            "probe_status": probe_status,
                        }
                    }
                probe_status["codex_proxy"] = "supported"
                # The proxy has no key of its own: it presents this token, and
                # its upstream config supplies the real one.
                #
                # The ``/v1`` segment is required, not cosmetic: the probe below
                # appends only ``/responses`` (as codex itself does), while the
                # proxy serves at ``/v1/responses``. Without it the probe hits
                # the proxy's own router and gets a bare FastAPI 404 -- a false
                # red on the exact config the proxy exists to fix.
                probe_base_url = (
                    str(codex_proxy_state.get("host_base_url") or "").rstrip("/") + "/v1"
                )
                probe_api_key = str(codex_proxy_state.get("token") or "")
            profile = {
                "engine": "codex",
                "transport": "codex_cli",
                "credential_mode": "api",
                "base_url": probe_base_url,
                # Pinned: the health probe asks /responses directly, so a stale
                # `chat_completions` value must not steer it elsewhere.
                "wire_api": "responses",
                "model": item.model_name or "",
                "reasoning_effort": (item.capabilities_json or {}).get("reasoning_effort") or "medium",
            }
            probe_env = dict(os.environ)
            probe_env["OPENAI_API_KEY"] = probe_api_key
            (
                ok,
                detail,
                codex_timed_out,
                cli_available,
                codex_error_code,
            ) = await _run_codex_health_probe(
                profile, probe_env, _remaining_seconds(deadline)
            )
            timed_out = timed_out or codex_timed_out
            probe_status["codex_api_health"] = (
                "timeout" if codex_timed_out else "supported" if ok else "failed"
            )
            probe_status["codex_cli_version"] = (
                "supported" if cli_available else "unavailable"
            )
            capabilities.update(
                {
                    "reachable": ok,
                    "normal_chat": ok,
                    "cli_available": cli_available,
                    "wire_api": str(profile["wire_api"]),
                    "error_code": codex_error_code,
                    "error": "" if ok else (detail or "Codex API endpoint probe failed")[:400],
                    "latency": round((time.perf_counter() - started) * 1000),
                    "latency_ms": round((time.perf_counter() - started) * 1000),
                    "recommended_protocol": "codex_cli",
                    "timed_out": codex_timed_out,
                    "probe_status": dict(probe_status),
                    # Surfaced explicitly rather than left implicit in the seed:
                    # the settings page reads it to keep the switch on, and a
                    # probe must never look like it turned the routing off.
                    "route_via_proxy": bool(capabilities.get("route_via_proxy")),
                }
            )
            item.capabilities_json = capabilities
            item.supports_json_schema = False
            item.supports_json_object = False
            item.supports_native_tool_call = True
            item.last_test_at = datetime.now(UTC)
            item.last_test_ok = ok
            await session.commit()
            message = (
                "Codex API 能力测试完成"
                if ok
                else "Codex API 测试超时（未在预算内完成）"
                if codex_timed_out
                else "Codex API 测试失败"
            )
            return {
                "data": {
                    "ok": ok,
                    "message": message,
                    "capabilities": capabilities,
                    "elapsed_ms": capabilities["latency_ms"],
                    "timed_out": codex_timed_out,
                    "incomplete_probe_count": int(codex_timed_out),
                    "probe_status": probe_status,
                }
            }
        executable = os.environ.get("MUTEKI_CODEX_BIN", "").strip() or shutil.which("codex")
        version = ""
        error = ""
        cli_timed_out = False
        if executable:
            remaining = _remaining_seconds(deadline)
            if remaining <= 0:
                cli_timed_out = True
                error = "Codex CLI version probe skipped: deadline exhausted"
            else:
                try:
                    returncode, stdout, stderr, cli_timed_out = await _run_async_process(
                        [executable, "--version"],
                        timeout=min(CODEX_CLI_PROBE_TIMEOUT_SECONDS, remaining),
                    )
                    version = (stdout or stderr or "").strip()[-300:]
                    capabilities["cli_available"] = returncode == 0 and not cli_timed_out
                    if cli_timed_out:
                        error = f"Codex CLI version probe timed out (>{CODEX_CLI_PROBE_TIMEOUT_SECONDS:g}s)"
                    elif returncode != 0:
                        error = version or f"codex exited with {returncode}"
                except OSError as exc:
                    error = type(exc).__name__
        else:
            error = "codex executable not found"
        timed_out = timed_out or cli_timed_out
        probe_status["codex_cli_version"] = (
            "timeout"
            if cli_timed_out
            else "supported"
            if executable and capabilities.get("cli_available")
            else "unavailable"
        )
        capabilities.update(
            {
                "reachable": bool(executable and capabilities.get("cli_available")),
                "normal_chat": bool(executable and capabilities.get("cli_available")),
                "cli_executable": executable or "",
                "cli_version": version,
                "model": item.model_name,
                "reasoning_effort": (item.capabilities_json or {}).get("reasoning_effort", "medium"),
                "error_code": (
                    None
                    if executable and capabilities.get("cli_available")
                    else "CODEX_CLI_PROBE_TIMEOUT"
                    if cli_timed_out
                    else "CODEX_CLI_UNAVAILABLE"
                ),
                "error": error,
                "latency": round((time.perf_counter() - started) * 1000),
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "recommended_protocol": "codex_cli",
                "timed_out": cli_timed_out,
                "probe_status": dict(probe_status),
            }
        )
        item.capabilities_json = capabilities
        item.supports_json_schema = False
        item.supports_json_object = False
        item.supports_native_tool_call = True
        item.last_test_at = datetime.now(UTC)
        item.last_test_ok = bool(capabilities["reachable"])
        await session.commit()
        message = (
            "Codex CLI 测试超时（未在预算内完成）"
            if cli_timed_out
            else "Codex CLI 能力测试完成"
        )
        return {
            "data": {
                "ok": item.last_test_ok,
                "message": message,
                "capabilities": capabilities,
                "elapsed_ms": capabilities["latency_ms"],
                "timed_out": cli_timed_out,
                "incomplete_probe_count": int(cli_timed_out),
                "probe_status": probe_status,
            }
        }
    headers = {"Authorization": f"Bearer {decrypt_api_key(item.encrypted_api_key)}"}
    # OpenAI-wire providers may serve either dialect.  Resolve the endpoint once
    # (so a base_url that already carries a path is not doubled) and render every
    # probe payload in the configured dialect.
    wire_api = normalize_wire_api(getattr(item, "wire_api", None), default=CHAT_COMPLETIONS)
    endpoint = resolve_endpoint(str(item.base_url or ""), wire_api)

    def outbound(payload: dict) -> dict:
        if wire_api == RESPONSES:
            return to_responses_request(payload)
        return payload

    try:
        async with httpx.AsyncClient(timeout=MODEL_TEST_BASE_TIMEOUT_SECONDS, trust_env=False) as client:
            base_payload = {
                "model": item.model_name,
                "messages": [{"role": "user", "content": "Reply with a short health response."}],
                "max_tokens": min(item.max_output_tokens, 32),
                "temperature": item.temperature,
            }
            try:
                response, negotiated = await _post_probe_negotiating(
                    client,
                    endpoint,
                    headers=headers,
                    payload=outbound(base_payload),
                    deadline=deadline,
                    configured_timeout=min(
                        float(item.request_timeout_seconds),
                        MODEL_TEST_BASE_TIMEOUT_SECONDS,
                    ),
                )
                body = response.json()
                if wire_api == RESPONSES and isinstance(body, dict):
                    # Present the reply in the Chat shape the rest of this
                    # function (usage, capability names) already understands.
                    body = responses_to_chat_body(body)
            except _ProbeTimedOut as error:
                timed_out = True
                probe_status["normal_chat"] = "timeout"
                capabilities["error_code"] = "MODEL_TEST_TIMEOUT"
                capabilities["timed_out_phase"] = "normal_chat"
                capabilities["error"] = str(error)
            except httpx.HTTPStatusError as error:
                probe_status["normal_chat"] = "failed"
                capabilities["error_code"] = {
                    400: "MODEL_BAD_REQUEST",
                    401: "MODEL_AUTH_FAILED",
                    402: "MODEL_QUOTA_EXCEEDED",
                    403: "MODEL_PERMISSION_DENIED",
                    429: "MODEL_RATE_LIMITED",
                }.get(error.response.status_code, "MODEL_UNAVAILABLE")
                capabilities["quota_state"] = (
                    "exceeded"
                    if error.response.status_code == 402
                    else capabilities["quota_state"]
                )
                capabilities["rate_limit_state"] = (
                    "limited"
                    if error.response.status_code == 429
                    else capabilities["rate_limit_state"]
                )
                capabilities["error"] = str(error)
            except (httpx.HTTPError, ValueError) as error:
                probe_status["normal_chat"] = "unavailable"
                capabilities["error_code"] = "MODEL_UNAVAILABLE"
                capabilities["error"] = str(error)
            else:
                capabilities["reachable"] = capabilities["normal_chat"] = True
                capabilities["parameter_constraints"] = negotiated
                probe_status["normal_chat"] = "supported"
                capabilities["usage"] = body.get("usage", {})
                capabilities["latency"] = round((time.perf_counter() - started) * 1000)
                probe_results = await _probe_openai_capabilities(
                    client,
                    url=endpoint,
                    headers=headers,
                    base_payload=base_payload,
                    deadline=deadline,
                    wire_api=wire_api,
                )
                for probe in probe_results:
                    name = str(probe["name"])
                    status = str(probe["status"])
                    capabilities[name] = bool(probe["ok"])
                    probe_status[name] = status
                    timed_out = timed_out or status == "timeout"
                    if status == "rate_limited":
                        capabilities["rate_limit_state"] = "limited"
                        if probe.get("retry_after"):
                            capabilities["retry_after"] = probe["retry_after"]
    except (httpx.HTTPError, OSError, ValueError) as error:
        capabilities["error_code"] = "MODEL_UNAVAILABLE"
        capabilities["error"] = str(error)
    capabilities["timed_out"] = timed_out
    capabilities["probe_status"] = dict(probe_status)
    incomplete_probe_count = sum(status == "timeout" for status in probe_status.values())
    capabilities["incomplete_probe_count"] = incomplete_probe_count
    capabilities["latency_ms"] = round((time.perf_counter() - started) * 1000)
    if capabilities.get("json_schema"):
        capabilities["recommended_protocol"] = "json_schema"
    elif capabilities.get("json_object"):
        capabilities["recommended_protocol"] = "json_object"
    else:
        capabilities["recommended_protocol"] = "prompt_json"
    item.capabilities_json = capabilities
    item.supports_json_schema = bool(capabilities.get("json_schema"))
    item.supports_json_object = bool(capabilities.get("json_object"))
    item.supports_native_tool_call = bool(capabilities.get("native_tool_call"))
    item.last_test_at = datetime.now(UTC)
    item.last_test_ok = bool(capabilities["reachable"] and capabilities["normal_chat"])
    await session.commit()
    message = (
        "模型连接测试完成；部分能力探测超出预算"
        if item.last_test_ok and timed_out
        else "模型连接测试超时（未在预算内完成）"
        if timed_out
        else "模型能力测试完成"
        if item.last_test_ok
        else "模型连接测试失败"
    )
    return {
        "data": {
            "ok": item.last_test_ok,
            "message": message,
            "capabilities": capabilities,
            "elapsed_ms": capabilities["latency_ms"],
            "timed_out": timed_out,
            "incomplete_probe_count": incomplete_probe_count,
            "probe_status": probe_status,
        }
    }
