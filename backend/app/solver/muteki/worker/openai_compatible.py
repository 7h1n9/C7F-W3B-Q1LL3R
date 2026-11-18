"""OpenAI-compatible Muteki Worker.

This adapter uses the official Muteki Chat Completions client for model turns
and keeps target-side HTTP execution inside the run-scoped Sandbox. It is a
Worker boundary, not a second Coordinator: the model chooses one bounded
action, the executor performs it, and the safe observation is written to the
official SharedGraph.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from muteki.core.cost import CostController
from muteki.core.llm import LLMClient
from muteki.core.wire import normalize_wire_api
from muteki.solver.cli_driver import CliDriver, CliResult
from muteki.solver.container_exec import run_cli_streaming_container

from app.core.llm_parameters import parameter_constraint, provider_response_body

from ..outcomes import DeadEndKind, DeadEndSignal, route_key, sanitize_dead_end_reason
from ..redaction import COMMAND_LIMIT, RESULT_LIMIT, redact
from .public_poc import lookup_public_poc

_SESSION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_FLAG_RE = re.compile(r"^flag\{[^}\r\n]{1,200}\}$")
_SAFE_HEADERS = frozenset({"accept", "content-type", "user-agent"})

# A script action is one bounded turn, exactly like a single HTTP request: it
# runs inside the same run-scoped Sandbox (`run_cli_streaming_container`, the
# transport `_http` already uses), so it adds no new trust boundary.  The caps
# below exist to keep one turn bounded, not to sandbox the process -- the CLI
# Workers already execute arbitrary shell in this same container.
_PYTHON_RUN_MAX_CODE = 250_000
_PYTHON_RUN_BUDGET = 8
_PYTHON_RUN_MAX_SECONDS = 120
_PYTHON_RUN_MAX_OUTPUT = 12_000

#: Outbound CVE lookups are slow (two APIs) and rate limited, so one Worker
#: turn gets a handful, not an unbounded search loop.
_LOOKUP_BUDGET = 4
_LOOKUP_TIMEOUT_SECONDS = 9.0

# Route identity deliberately ignores payload values, which is what makes the
# dedupe cheap -- and also what makes an injection payload look like a repeat.
# Say so explicitly, otherwise the model keeps re-sending it and gives up.
_VARY_PAYLOAD_HINT = (
    "This exact endpoint already has an observation. Payload variants (SQL "
    "injection, brute force, fuzzing one parameter) are not a new endpoint: use "
    "python_run to loop over the variants in one script."
)

#: Provider failures are collapsed into one code per *class* of cause, because
#: the alternative -- a bare ``FAILED`` with zero steps -- is indistinguishable
#: from "the model chose not to call a tool" and cost a full round of guessing
#: (see docs: an OpenAI-compatible Worker returning ``tool_steps: 0``).
_PROVIDER_ERROR_CODES = {
    401: "OPENAI_COMPATIBLE_PROVIDER_DENIED",
    403: "OPENAI_COMPATIBLE_PROVIDER_DENIED",
    429: "OPENAI_COMPATIBLE_PROVIDER_RATE_LIMITED",
}


def provider_failure_code(error: BaseException) -> str:
    """Name the provider-side failure so a Worker round is never a black box."""

    import httpx

    status = 0
    current: BaseException | None = error
    for _ in range(8):
        if current is None:
            break
        if isinstance(current, httpx.HTTPStatusError):
            status = int(getattr(current.response, "status_code", 0) or 0)
            break
        current = current.__cause__ or current.__context__
    if status >= 500:
        return "OPENAI_COMPATIBLE_PROVIDER_UNAVAILABLE"
    if status:
        return _PROVIDER_ERROR_CODES.get(status, "OPENAI_COMPATIBLE_PROVIDER_REJECTED")
    return "OPENAI_COMPATIBLE_WORKER_FAILED"


def _bounded_provider_detail(error: BaseException) -> str:
    body = provider_response_body(error) or str(error)
    return " ".join(body.split())[:300]


WORKER_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": "http_request",
            "description": "Perform one bounded HTTP request against the declared target.",
            "parameters": {
                "type": "object",
                "properties": {
                    "method": {"type": "string", "enum": ["GET", "POST", "PUT"]},
                    "url": {"type": "string"},
                    "params": {"type": "object"},
                    "headers": {"type": "object"},
                    "body": {"type": "string"},
                    "session_name": {"type": "string"},
                },
                "required": ["method", "url"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_session_request",
            "description": "Perform one HTTP request while retaining the named session cookie jar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "method": {"type": "string", "enum": ["GET", "POST", "PUT"]},
                    "url": {"type": "string"},
                    "params": {"type": "object"},
                    "headers": {"type": "object"},
                    "body": {"type": "string"},
                    "session_name": {"type": "string"},
                },
                "required": ["method", "url", "session_name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "python_run",
            "description": (
                "Run a bounded Python 3 script inside the run sandbox and receive its "
                "stdout/stderr. Use this for anything that needs more than one request: "
                "loops, encoding/decoding, parsing, and character-by-character oracles "
                "(e.g. boolean-blind extraction, which needs hundreds of requests and is "
                "impossible with http_request alone). Requests still have to target the "
                "declared target host. The script runs with the run workspace as its "
                "working directory, so it can read and write files there. Python 3 with "
                "the `requests` library is available. Print only what you need to see: "
                "stdout is truncated."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Python 3 source executed as a file in the sandbox.",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 5,
                        "maximum": 120,
                        "description": "Wall-clock limit for the script. Default 60.",
                    },
                },
                "required": ["code"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_flag",
            "description": "Submit a flag only when the exact value was observed in a real HTTP response or script output.",
            "parameters": {
                "type": "object",
                "properties": {"flag": {"type": "string"}},
                "required": ["flag"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_public_poc",
            "description": (
                "Look up public CVE records and PoC repositories by product, "
                "version or CVE id. Read-only and host-side: nothing is "
                "downloaded or executed. "
                "Match on this target's OWN stack: pass 'product major.minor.patch' "
                "taken from a banner/header/error page you actually observed, so the "
                "answer is filtered by that version's CPE. Read match_on.axis in the "
                "reply -- cpe_version means NVD lists your version as affected, "
                "keyword_product_only means only the product name matched and every "
                "record is unconfirmed here. Do not call this before you have "
                "fingerprinted the target: a product-wide search returns dozens of "
                "irrelevant CVEs and costs a round. Also use it for a *technique* "
                "when you already have code execution but cannot read the flag, e.g. "
                "'linux privilege escalation 4.19' or 'linux suid enumeration' -- "
                "that returns the harness repositories instead of a CVE. "
                "A match is a hypothesis, not proof."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "Product, framework or version, e.g. 'apache struts 2.5.20'.",
                    },
                    "cve_id": {
                        "type": "string",
                        "description": "Optional exact CVE id, e.g. CVE-2021-44228.",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    },
)

# Backwards-compatible name: this tuple used to hold only HTTP actions.
HTTP_TOOLS = WORKER_TOOLS


class _ContainerCommandDriver(CliDriver):
    """Minimal parser used only to run a command through Muteki's Sandbox RCP."""

    name = "shell"

    def build_execute(self, prompt, session, *, web_access=True, kb_access=True, stream=False):
        del prompt, session, web_access, kb_access, stream
        return []

    def build_resume(self, prompt, session, *, web_access=True, kb_access=True, stream=False):
        del prompt, session, web_access, kb_access, stream
        return []

    def parse(self, stdout: str, stderr: str) -> CliResult:
        return CliResult(text=stdout, raw_stderr=stderr)


class OpenAICompatibleWorker:
    """One bounded Chat Completions Worker over the canonical Muteki graph."""

    def __init__(
        self,
        *,
        graph: Any,
        container: Any,
        workspace: str,
        worker_id: str,
        intent_id: str | None,
        target_url: str,
        base_url: str,
        api_key: str,
        model: str,
        max_turns: int,
        timeout_seconds: int,
        cost: CostController,
        run_id: str,
        wire_api: str = "chat_completions",
        step_callback: Callable[[dict[str, str]], None] | None = None,
        challenge_brief: Sequence[str] | None = None,
    ) -> None:
        self.graph = graph
        self.container = container
        self.workspace = Path(workspace).resolve()
        self.worker_id = worker_id
        self.intent_id = intent_id
        # Which OpenAI dialect the relay serves.  A Responses-only relay rejects
        # a Chat body outright, so this has to travel with the job rather than
        # being assumed.
        self.wire_api = normalize_wire_api(wire_api)
        self.target_url = target_url.rstrip("/") + "/"
        self.target_host = (urlsplit(target_url).hostname or "").casefold()
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_turns = max(1, min(int(max_turns), 24))
        self.timeout_seconds = max(5, min(int(timeout_seconds), 120))
        self.cost = cost
        self.run_id = run_id
        # Observability sink, mirroring the CLI Worker's ``step_callback``.  This
        # engine does not go through ``CliSolver``/``driver.parse_stream_steps``
        # at all (see ``_execute_sync``'s ``chat_completions`` fork), so without
        # its own reporting point a Chat Completions Worker stays a black box and
        # ``WORKER_STEP`` is never written for it.
        self.step_callback = step_callback
        # What the Challenge record declares about its own surface (endpoint,
        # injectable parameter, fixed control values).  The CLI Worker receives
        # the same lines through the workspace ``AGENTS.md``; this path never
        # reads that file, so without it the only parameter names a Worker ever
        # saw were the ones the Reason model invented.
        self.challenge_brief = [
            str(item).strip() for item in (challenge_brief or ()) if str(item).strip()
        ][:24]
        self.records: list[dict[str, Any]] = []
        self.verified_flag: str | None = None
        self.dead_end_signal: DeadEndSignal | None = None
        self._request_count = 0
        self._new_endpoint_count = 0
        self._python_run_count = 0
        self._lookup_count = 0
        self._visited_endpoint_keys = self._load_visited_endpoint_keys()
        self._duplicate_request_count = 0
        self._consecutive_duplicate_requests = 0
        self.route_exhausted = False
        self._cancel_event = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[Any] | None = None
        # Model-owned parameter values learned from a refusal.  ``None`` means
        # "the upstream named the field but allows no value", i.e. drop it.
        # Kept for the whole round so the negotiation costs one extra request
        # in total, not one per turn.
        self._parameter_overrides: dict[str, float | None] = {}
        self._last_provider_error = ""
        self._last_finish_reason = ""

    def cancel(self) -> None:
        """Cancel the current model turn from the Coordinator thread."""

        self._cancel_event.set()
        loop = self._loop
        task = self._task
        if loop is not None and task is not None and not task.done():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass

    def _cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def _turn_payload(self) -> dict[str, Any]:
        """Turn parameters after the overrides learned this round."""

        payload: dict[str, Any] = {"temperature": 0.0, "max_tokens": 4000}
        for field, value in self._parameter_overrides.items():
            if value is None:
                payload.pop(field, None)
            else:
                payload[field] = value
        return payload

    async def _chat_turn(self, client: LLMClient, messages: list[dict[str, Any]]):
        """One model turn that survives a model-owned parameter refusal.

        Measured on ``kimi-k3``: the relay answers
        ``400 field temperature invalid, only 1 is allowed for this model``
        for ``temperature: 0.0``.  That is a fact about *that model*, not a
        defect in the payload -- so the Worker negotiates once, exactly like
        the Reason planner does, instead of failing every turn of the round.
        A refusal that is *not* about one negotiable parameter is re-raised:
        hiding a real outage behind a retry is worse than reporting it.
        """

        try:
            return await client.chat(
                model=self.model,
                messages=messages,
                tools=list(WORKER_TOOLS),
                stream=False,
                run_id=self.run_id,
                challenge_id=self.run_id,
                solver_id=self.worker_id,
                **self._turn_payload(),
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            constraint = parameter_constraint(error)
            if constraint is None:
                raise
            field, value = constraint
            if field in self._parameter_overrides and self._parameter_overrides[field] == value:
                # Already negotiated this exact value and the upstream refused
                # it again: the failure is real, do not loop.
                raise
            self._parameter_overrides[field] = value
            self._report_step("note", f"PARAMETER_NEGOTIATED:{field}")
            return await client.chat(
                model=self.model,
                messages=messages,
                tools=list(WORKER_TOOLS),
                stream=False,
                run_id=self.run_id,
                challenge_id=self.run_id,
                solver_id=self.worker_id,
                **self._turn_payload(),
            )

    def _report_step(
        self,
        kind: str,
        tool: str = "",
        *,
        command: str = "",
        result_excerpt: str = "",
        call_id: str = "",
    ) -> None:
        """Report one Worker step through the Coordinator's observability sink.

        ``command`` and ``result_excerpt`` are what make a failed round
        reviewable; they are redacted at this boundary (see
        :mod:`muteki.redaction`) and bounded before they ever reach the event
        stream.  Everything else on the payload stays identity/progress only.

        Failure-isolated on purpose: a broken sink must degrade to silence
        rather than abort a Worker mid-round.
        """

        callback = self.step_callback
        if callback is None:
            return
        payload: dict[str, str] = {
            "worker_id": self.worker_id,
            "kind": str(kind),
            "tool": str(tool),
            "session": "",
        }
        if call_id:
            payload["call_id"] = str(call_id)
        if command:
            payload["command"] = redact(command, limit=COMMAND_LIMIT)
        if result_excerpt:
            payload["result_excerpt"] = redact(result_excerpt, limit=RESULT_LIMIT)
        try:
            callback(payload)
        except Exception:
            pass

    async def run(self, *, goal: str, payload: Mapping[str, Any] | None = None):
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        if self.container is None:
            return self._result(False, "CONTAINER_NOT_READY")
        if not self.base_url or not self.api_key or not self.model:
            return self._result(False, "OPENAI_COMPATIBLE_PROFILE_INCOMPLETE")

        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "You are a Muteki web-pentest Worker. Read the supplied SharedGraph "
                    "summary, then make bounded evidence-driven HTTP requests against "
                    "the declared target. Do not read challenge source files or invent "
                    "a flag. Use submit_flag only after the exact flag appears in a "
                    "real HTTP response or in python_run output. Code execution is a "
                    "means, not the goal: once a webshell or command runs, use it to "
                    "read the flag and submit what that response contained. Continue "
                    "exploring "
                    "after a login page; do not stop after one request. Never repeat a "
                    "visited endpoint; if the assigned route has no new endpoint, "
                    "report ROUTE_EXHAUSTED. When a technique needs many requests in a "
                    "row (blind boolean/character extraction, brute force, replaying a "
                    "session across steps), write one python_run script that performs "
                    "the whole loop and prints the result, instead of issuing the same "
                    "request over and over with http_request. If the payload carries "
                    "challenge_declared_surface, those endpoint paths, parameter names "
                    "and control values are the Challenge's own statements: use them "
                    "verbatim and do not substitute names you think are more likely. "
                    "When the target reveals a specific product, framework, CMS or "
                    "version string, call lookup_public_poc once for it instead of "
                    "guessing an exploit path from memory; a returned CVE is a "
                    "hypothesis to confirm against this target, never proof. "
                    # The fingerprint loop, stated as an order because it is the
                    # one thing an unannotated box needs and the thing a Worker
                    # most reliably skips.  Measured on the arena's ordinary Web
                    # boxes (no CVE id, no advisory number in the title): with no
                    # recipe and no instruction, the round degrades into header
                    # and robots.txt reading until the window closes.
                    #
                    # Kept to one pass on purpose.  A second fingerprint sweep
                    # costs a minute and tells the Worker nothing new; the point
                    # is to spend that minute on the first request, not to
                    # enumerate the stack.
                    "FINGERPRINT BEFORE YOU GUESS: on your first turn, in ONE "
                    "combined command, collect (a) the Server / X-Powered-By / "
                    "Set-Cookie headers, (b) any product or version string, and "
                    "(c) the one or two paths or parameters that look like the "
                    "application's own surface. If (b) names a product AND a "
                    "version, immediately call lookup_public_poc with that exact "
                    "'product version' -- do this in the same turn, not after "
                    "another exploration step. If the reply carries "
                    "curated_recipes, that is the recipe for the CVE you just "
                    "discovered: follow its first_action next. Do not repeat the "
                    "fingerprint sweep; one pass, then act."
                ),
            },
            {
                "role": "user",
                "content": self._initial_prompt(goal, payload or {}),
            },
        ]
        client = LLMClient(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=float(self.timeout_seconds),
            overall_timeout=float(self.timeout_seconds),
            cost=self.cost,
            wire_api=self.wire_api,
        )
        try:
            for _ in range(self.max_turns):
                if self._cancelled():
                    return self._result(False, "INTERRUPTED")
                self._report_step("turn")
                response = await self._chat_turn(client, messages)
                if not response.tool_calls:
                    # Say *why* the round stopped here.  Without this the event
                    # stream shows `tool_steps: 0, steps_total: 1, FAILED`
                    # whether the model answered with prose, hit a token cap,
                    # or the endpoint silently dropped the tool schema.
                    self._last_finish_reason = str(
                        getattr(response, "finish_reason", "") or ""
                    )
                    self._last_provider_error = (
                        f"NO_TOOL_CALL finish_reason={self._last_finish_reason or 'unknown'}"
                        f" content_chars={len(str(getattr(response, 'content', '') or ''))}"
                    )
                    self._report_step("note", "NO_TOOL_CALL")
                    break
                assistant_calls = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }
                    for call in response.tool_calls
                ]
                messages.append(
                    {
                        "role": "assistant",
                        "content": response.content or None,
                        "tool_calls": assistant_calls,
                    }
                )
                for call in response.tool_calls:
                    if self._cancelled():
                        return self._result(False, "INTERRUPTED")
                    arguments, parse_error = _parse_tool_arguments(call.arguments)
                    if parse_error:
                        # Tell the model precisely what is wrong; a silent empty
                        # argument set made it repeat the same call verbatim.
                        result: dict[str, Any] = {
                            "success": False,
                            "error": parse_error,
                            "hint": (
                                "Arguments must be one JSON object. For multi-line "
                                "code keep the newlines escaped (\\n) inside the "
                                "JSON string; do not paste raw newlines."
                            ),
                        }
                        self._report_step(
                            "tool",
                            call.name,
                            command=str(call.arguments or ""),
                            result_excerpt=str(parse_error),
                            call_id=str(call.id or ""),
                        )
                    else:
                        result = await self._dispatch_tool_observed(
                            call.name, arguments, call_id=str(call.id or "")
                        )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
                    if self.verified_flag or self.dead_end_signal is not None or self.route_exhausted:
                        break
                if self.verified_flag or self.dead_end_signal is not None or self.route_exhausted:
                    break
        except asyncio.CancelledError:
            return self._result(False, "INTERRUPTED")
        except Exception as error:
            # A provider-side failure has to leave a trace in the event stream,
            # otherwise the Coordinator only ever sees a zero-step FAILED.
            code = provider_failure_code(error)
            self._last_provider_error = _bounded_provider_detail(error)
            self._report_step("error", code)
            return self._result(
                False,
                code,
                error_type=type(error).__name__,
                provider_detail=self._last_provider_error,
            )
        finally:
            await client.aclose()
            self._task = None
            self._loop = None
        if self._duplicate_request_count and self._new_endpoint_count == 0 and not self.verified_flag:
            self.route_exhausted = True
        if self.route_exhausted and not self.verified_flag:
            return self._result(False, "ROUTE_EXHAUSTED", route_exhausted=True)
        if not self.records:
            return self._result(
                False,
                "OPENAI_COMPATIBLE_NO_TOOL_CALL",
                provider_detail=self._last_provider_error,
                finish_reason=self._last_finish_reason,
            )
        return self._result(True, "COMPLETED")

    async def _dispatch_tool_observed(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        call_id: str = "",
    ) -> dict[str, Any]:
        """Dispatch one tool call and publish it as a single complete record.

        Keeping dispatch and observation together is what makes "every tool
        call is logged" a property of the code rather than of each call site:
        the command and the result are reported once, together, after the result
        exists.  Splitting them (one event before the call, one after) used to
        leave two half-records that no reviewer could reliably pair up.
        """

        result = await self._dispatch_tool(name, arguments)
        self._report_step(
            "tool",
            name,
            command=_command_preview(name, arguments),
            result_excerpt=_result_preview(result),
            call_id=call_id,
        )
        return result

    async def _dispatch_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        # Every bounded action funnels through here.  Observability is emitted by
        # _dispatch_tool_observed, which wraps this so each invocation produces
        # exactly one record carrying both the command and its output.
        if name in {"http_request", "http_session_request"}:
            return await self._http(name, arguments)
        if name == "python_run":
            return await self._python_run(arguments)
        if name == "lookup_public_poc":
            return await self._lookup_public_poc(arguments)
        if name == "submit_flag":
            return self._submit_flag(arguments)
        if name == "report_dead_end":
            return self._report_dead_end(arguments)
        return {"success": False, "error": "UNSUPPORTED_TOOL"}

    async def _http(self, tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        method = str(arguments.get("method") or "GET").upper()
        url = self._target_url(str(arguments.get("url") or "/"))
        if not url:
            return {"success": False, "error": "TARGET_HOST_NOT_ALLOWED"}
        endpoint_key = self._endpoint_key(
            tool_name,
            method,
            url,
            arguments.get("params"),
            arguments.get("session_name"),
        )
        # Race Workers start together. Refresh the shared Blackboard before
        # every request so a sibling's newly written fact is not hidden by
        # this Worker's construction-time snapshot.
        self._visited_endpoint_keys.update(self._load_visited_endpoint_keys())
        if endpoint_key in self._visited_endpoint_keys:
            self._duplicate_request_count += 1
            self._consecutive_duplicate_requests += 1
            self.route_exhausted = self._consecutive_duplicate_requests >= 2
            return {
                "success": False,
                "error": "ROUTE_EXHAUSTED",
                "route_exhausted": self.route_exhausted,
                "endpoint": endpoint_key,
                "hint": _VARY_PAYLOAD_HINT,
            }
        session_name = str(arguments.get("session_name") or "default")
        if not _SESSION_RE.fullmatch(session_name):
            return {"success": False, "error": "INVALID_SESSION_NAME"}
        self._request_count += 1
        if self._request_count > 16:
            return {
                "success": False,
                "error": "REQUEST_BUDGET_EXHAUSTED",
                "hint": (
                    "One request per call is not the right shape for this step. Use "
                    "python_run to run a loop that performs the whole sequence."
                ),
            }
        activity_key = f"http:{endpoint_key}"
        activity_claimed = self._claim_endpoint_activity(activity_key)
        if not activity_claimed:
            self._request_count -= 1
            self._duplicate_request_count += 1
            self._consecutive_duplicate_requests += 1
            # A sibling executed this endpoint and kept the lease.  That used to
            # end the round outright, which killed a freshly assigned Worker on
            # its very first call whenever the Coordinator handed it a route an
            # earlier Worker had touched.  Give it the same two-strike rule as
            # the visited-set branch and point it at python_run, which needs no
            # per-endpoint lease.
            self.route_exhausted = self._consecutive_duplicate_requests >= 2
            return {
                "success": False,
                "error": "ENDPOINT_ALREADY_EXECUTED",
                "route_exhausted": self.route_exhausted,
                "endpoint": endpoint_key,
                "hint": _VARY_PAYLOAD_HINT,
            }
        # A sibling may have completed this endpoint between our initial
        # Blackboard read and the activity-lock claim.  The upstream Muteki
        # activity lock prevents concurrent execution, but the lock is
        # deliberately released after a successful observation; re-read the
        # durable fact after winning the lock so the next Worker cannot start
        # the same request in that hand-off window.  This keeps route-level
        # deduplication in the shared Graph instead of adding local state.
        if endpoint_key in self._load_visited_endpoint_keys():
            self._release_endpoint_activity(activity_key, activity_claimed)
            self._request_count -= 1
            self._duplicate_request_count += 1
            self._consecutive_duplicate_requests += 1
            self.route_exhausted = self._consecutive_duplicate_requests >= 2
            return {
                "success": False,
                "error": "ROUTE_EXHAUSTED",
                "route_exhausted": self.route_exhausted,
                "endpoint": endpoint_key,
                "hint": _VARY_PAYLOAD_HINT,
            }
        self._visited_endpoint_keys.add(endpoint_key)
        self._new_endpoint_count += 1
        self._consecutive_duplicate_requests = 0
        # Keep the Muteki activity reservation after an observation.  The
        # upstream activity lock is a route-level "already executed" marker,
        # not only an in-flight mutex; releasing it after every successful
        # request lets one model turn issue the same endpoint repeatedly.
        keep_activity_claim = False
        headers = {
            str(key).lower(): str(value)[:200]
            for key, value in (arguments.get("headers") or {}).items()
            if str(key).lower() in _SAFE_HEADERS
        }
        config = {
            "method": method,
            "url": url,
            "params": _safe_mapping(arguments.get("params")),
            "headers": headers,
            "body": str(arguments.get("body") or "")[:12000],
            "session_name": session_name,
            "session_path": "/home/kali/workspace/.muteki-chat-sessions",
        }
        encoded = base64.b64encode(json.dumps(config, ensure_ascii=False).encode()).decode()
        cancel_event = threading.Event()
        command = asyncio.create_task(
            asyncio.to_thread(
                run_cli_streaming_container,
                _ContainerCommandDriver(),
                ["python3", "-c", _HTTP_SCRIPT, encoded],
                handle=self.container,
                cwd=str(self.workspace),
                timeout=self.timeout_seconds,
                on_step=lambda _step: None,
                cancel_event=cancel_event,
            ),
            name=f"muteki-http-{self.worker_id}-{self._request_count}",
        )
        try:
            try:
                result = await asyncio.shield(command)
            except asyncio.CancelledError:
                cancel_event.set()
                try:
                    await asyncio.wait_for(
                        asyncio.shield(command),
                        timeout=max(1.0, min(15.0, float(self.timeout_seconds) + 1.0)),
                    )
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
                raise
            try:
                observed = json.loads(result.text or "{}")
            except (TypeError, ValueError):
                observed = {"success": False, "error": "HTTP_EXECUTION_OUTPUT_INVALID"}
            observed.setdefault("success", bool(result.text and not result.timed_out))
            body = str(observed.get("body") or "")
            record = {
                "tool": tool_name,
                "method": method,
                "url": url,
                "status_code": observed.get("status_code"),
                "final_url": observed.get("final_url"),
                "headers": observed.get("headers") or {},
                "body": body,
            }
            self.records.append(record)
            safe = self._write_safe_fact(
                tool_name,
                url,
                observed,
                method=method,
                params=arguments.get("params"),
                session_name=session_name,
            )
            # A valid HTTP response (including a target 4xx/5xx) is an
            # observation and therefore exhausts this normalized route.  A
            # sandbox/transport failure is not an observation; release the
            # lease so Muteki can recover or assign a different route.
            keep_activity_claim = bool(observed.get("success"))
            return {
                "success": bool(observed.get("success")),
                "status_code": observed.get("status_code"),
                "final_url": observed.get("final_url") or url,
                "headers": observed.get("headers") or {},
                "cookie_names": observed.get("cookie_names") or [],
                "body": body[:12000],
                "safe_observation": safe,
                "error": observed.get("error"),
            }
        finally:
            if not keep_activity_claim:
                self._release_endpoint_activity(activity_key, activity_claimed)

    async def _python_run(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Execute one bounded Python script inside the run Sandbox.

        This is the same transport ``_http`` uses (``run_cli_streaming_container``
        against the run's container handle), so the isolation boundary is
        unchanged.  What it adds is *iteration*: a blind oracle needs hundreds of
        requests, which a fixed per-turn request budget can never express.
        """

        code = str(arguments.get("code") or "")
        if not code.strip():
            return {
                "success": False,
                "error": "PYTHON_CODE_REQUIRED",
                "hint": "Pass Python source in the `code` field as a JSON string.",
            }
        if len(code) > _PYTHON_RUN_MAX_CODE:
            return {
                "success": False,
                "error": "PYTHON_CODE_TOO_LARGE",
                "limit": _PYTHON_RUN_MAX_CODE,
                "length": len(code),
            }
        self._python_run_count += 1
        if self._python_run_count > _PYTHON_RUN_BUDGET:
            return {
                "success": False,
                "error": "PYTHON_RUN_BUDGET_EXHAUSTED",
                "budget": _PYTHON_RUN_BUDGET,
                "hint": "Stop scripting and report what you have established so far.",
            }
        requested = arguments.get("timeout_seconds")
        try:
            script_timeout = int(requested) if requested is not None else 60
        except (TypeError, ValueError):
            script_timeout = 60
        script_timeout = max(5, min(script_timeout, _PYTHON_RUN_MAX_SECONDS))
        config = {
            "code": code,
            # The script inherits the exec's cwd, which
            # ``run_cli_streaming_container`` already translated into a container
            # path.  Sending the host workspace path here would be a path that
            # does not exist inside the container.
            "target": self.target_url,
            "timeout": script_timeout,
        }
        payload = base64.b64encode(json.dumps(config, ensure_ascii=False).encode()).decode()
        cancel_event = threading.Event()
        command = asyncio.create_task(
            asyncio.to_thread(
                run_cli_streaming_container,
                _ContainerCommandDriver(),
                ["python3", "-c", _PYTHON_RUN_SCRIPT, payload],
                handle=self.container,
                cwd=str(self.workspace),
                # Give the wrapper room to report its own timeout instead of
                # being killed by the supervisor at the same instant.
                timeout=script_timeout + 15,
                on_step=lambda _step: None,
                cancel_event=cancel_event,
            ),
            name=f"muteki-python-{self.worker_id}-{self._python_run_count}",
        )
        try:
            result = await asyncio.shield(command)
        except asyncio.CancelledError:
            cancel_event.set()
            try:
                await asyncio.wait_for(
                    asyncio.shield(command),
                    timeout=max(1.0, min(15.0, float(script_timeout) + 1.0)),
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            raise
        observed = _parse_trailing_json(result.text)
        if not isinstance(observed, Mapping):
            observed = {
                "success": False,
                "error": "PYTHON_RUN_OUTPUT_INVALID",
                "detail": (
                    "the sandbox returned no parseable result; the script never "
                    "started or the executor produced no output"
                ),
                "stdout": str(result.text or "")[:_PYTHON_RUN_MAX_OUTPUT],
                "stderr": str(result.raw_stderr or "")[:_PYTHON_RUN_MAX_OUTPUT],
            }
        stdout = str(observed.get("stdout") or "")[:_PYTHON_RUN_MAX_OUTPUT]
        stderr = str(observed.get("stderr") or "")[:_PYTHON_RUN_MAX_OUTPUT]
        # ``submit_flag`` proves a candidate came out of a real observation.  A
        # script's stdout is such an observation, so it has to be visible to
        # that check -- otherwise anything found by python_run could never be
        # submitted, which is the whole point of this action.
        self.records.append(
            {
                "tool": "python_run",
                "method": "SCRIPT",
                "url": self.target_url,
                "status_code": 0,
                "final_url": self.target_url,
                "headers": {},
                "body": f"{stdout}\n{stderr}",
            }
        )
        self._write_python_fact(stdout, stderr, observed)
        return {
            "success": bool(observed.get("success")),
            "exit_code": observed.get("exit_code"),
            "timed_out": bool(observed.get("timed_out")),
            "duration_seconds": observed.get("duration_seconds"),
            "stdout": stdout,
            "stderr": stderr,
            "error": observed.get("error") or None,
            "detail": observed.get("detail") or None,
            "cwd": observed.get("cwd"),
            "runs_used": self._python_run_count,
            "runs_left": max(0, _PYTHON_RUN_BUDGET - self._python_run_count),
        }

    async def _lookup_public_poc(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Look up public CVE metadata and PoC repositories, read-only.

        The result is written to the SharedGraph with ``verified=False`` on
        purpose.  A CVE that names the target's product is a hypothesis about
        what *might* be wrong; only the target's own response can confirm it.
        Publishing it as verified would let one Worker's lookup become another
        Worker's "evidence", which is exactly the kind of second-hand claim the
        flag rule exists to stop.
        """

        if self._lookup_count >= _LOOKUP_BUDGET:
            return {
                "success": False,
                "error": "LOOKUP_BUDGET_EXHAUSTED",
                "lookups_used": self._lookup_count,
                "hint": (
                    "Lookups are rate limited and are not evidence about this "
                    "target. Test the current hypothesis against the target."
                ),
            }
        keyword = str(arguments.get("keyword") or "").strip()
        cve_id = str(arguments.get("cve_id") or "").strip()
        if not keyword and not cve_id:
            return {"success": False, "error": "KEYWORD_OR_CVE_ID_REQUIRED"}
        self._lookup_count += 1
        result = await lookup_public_poc(
            keyword=keyword,
            cve_id=cve_id,
            timeout=_LOOKUP_TIMEOUT_SECONDS,
        )
        result["lookups_used"] = self._lookup_count
        result["lookups_left"] = max(0, _LOOKUP_BUDGET - self._lookup_count)
        self._attach_recipes(result)
        self._write_lookup_fact(result)
        return result

    def _attach_recipes(self, result: dict[str, Any]) -> None:
        """Attach the curated recipe for any CVE this lookup surfaced.

        Without this the lookup dead-ends: the Worker learns "CVE-2017-10271 is
        worth trying" and then has to go find a writeup and improvise the request,
        while the curated recipe for exactly that CVE sits in the playbook,
        unreachable because recipe selection only ever ran against the challenge
        title.

        Only the top-ranked CVEs get a recipe, and only when the playbook has one
        -- ``recipe_for_cve`` returns ``""`` for an unknown CVE rather than
        substituting a product neighbour.  Attached recipes are labelled in the
        payload so the model can tell "curated for this exact CVE" from "the NVD
        record".
        """

        cves = [item for item in (result.get("cves") or []) if isinstance(item, Mapping)]
        if not cves:
            return
        try:
            from app.services.known_exploit import first_action_for_cve, recipe_for_cve
        except Exception:  # noqa: BLE001 - knowledge must never end a turn
            return

        attached: list[dict[str, str]] = []
        for item in cves[:3]:
            cve_id = str(item.get("cve_id") or "").strip().upper()
            if not cve_id:
                continue
            try:
                recipe = recipe_for_cve(cve_id)
            except Exception:  # noqa: BLE001
                recipe = ""
            if not recipe:
                continue
            try:
                action = first_action_for_cve(cve_id)
            except Exception:  # noqa: BLE001
                action = ""
            attached.append(
                {
                    "cve_id": cve_id,
                    "first_action": action,
                    "recipe": recipe,
                }
            )
        if attached:
            result["curated_recipes"] = attached
            result["curated_recipes_note"] = (
                "curated_recipes are this project's own recipe for that exact "
                "CVE, not the NVD record. They are a hypothesis about this "
                "target: send the request and confirm against the live box."
            )
            self._publish_recipe_facts(attached)

    def _publish_recipe_facts(self, attached: list[dict[str, str]]) -> None:
        """Put the discovered CVE's first move on the shared board.

        The recipe *text* only reaches the Worker that made the call (it is in
        this tool's return value).  The next Worker -- and the Coordinator when it
        re-dispatches -- reads the board, and the board truncates a fact to 240
        characters, so the whole recipe would not survive.

        The first move does survive, and it is the part that changes behaviour:
        "CVE-2017-10271, start with POST /wls-wsat/CoordinatorPortType" is
        actionable on its own.  So the one-liner is published as its own fact,
        with the CVE id in the dedupe key so a second lookup does not stack
        duplicates.
        """

        add_evidence = getattr(self.graph, "add_evidence", None)
        if not callable(add_evidence):
            return
        for item in attached:
            cve_id = str(item.get("cve_id") or "")
            action = str(item.get("first_action") or "")
            if not cve_id or not action:
                continue
            try:
                add_evidence(
                    actor=self.worker_id,
                    source=self.worker_id,
                    fact=(
                        f"[lookup] curated recipe for {cve_id}: FIRST ACTION {action}"
                    )[:240],
                    verified=False,
                    confidence=0.4,
                    intent_id=self.intent_id,
                )
            except Exception:  # noqa: BLE001 - a reference lookup must not end a turn
                continue

    def _write_lookup_fact(self, result: Mapping[str, Any]) -> None:
        """Publish the lookup as an explicitly unverified external reference."""

        cves = [item for item in (result.get("cves") or []) if isinstance(item, Mapping)]
        repos = [
            item for item in (result.get("poc_repositories") or []) if isinstance(item, Mapping)
        ]
        fact = {
            "tool": "lookup_public_poc",
            "success": bool(result.get("success")),
            "endpoint": "public-cve-database",
            "method": "LOOKUP",
            "keyword": str(result.get("keyword") or "")[:120],
            "cve_ids": [str(item.get("cve_id") or "") for item in cves][:4],
            "cvss": [item.get("cvss") for item in cves][:4],
            "poc_repositories": [str(item.get("repo") or "") for item in repos][:4],
            "error": str(result.get("error") or "")[:80],
        }
        add_evidence = getattr(self.graph, "add_evidence", None)
        if not callable(add_evidence):
            return
        try:
            add_evidence(
                actor=self.worker_id,
                source=self.worker_id,
                fact=json.dumps(fact, ensure_ascii=False, sort_keys=True),
                verified=False,
                confidence=0.3,
                intent_id=self.intent_id,
            )
        except Exception:  # noqa: BLE001 - a reference lookup must not end a turn
            return

    def _write_python_fact(
        self, stdout: str, stderr: str, observed: Mapping[str, Any]
    ) -> None:
        """Publish a script outcome to the SharedGraph as one verified fact."""

        exit_code = observed.get("exit_code")
        first_line = next(
            (line.strip() for line in stdout.splitlines() if line.strip()), ""
        )
        fact = {
            "tool": "python_run",
            "success": bool(observed.get("success")),
            "exit_code": exit_code if exit_code is not None else -1,
            "timed_out": bool(observed.get("timed_out")),
            "endpoint": "python",
            "method": "SCRIPT",
            "script_count": self._python_run_count,
            "error": str(observed.get("error") or "")[:80],
            "stdout_head": first_line[:180],
            "stderr_head": stderr.strip().splitlines()[0][:120] if stderr.strip() else "",
            "request_count": self._request_count,
        }
        add_evidence = getattr(self.graph, "add_evidence", None)
        if callable(add_evidence):
            add_evidence(
                actor=self.worker_id,
                source=self.worker_id,
                fact=json.dumps(fact, ensure_ascii=False, sort_keys=True),
                verified=True,
                confidence=0.7,
                intent_id=self.intent_id,
            )

    def _claim_endpoint_activity(self, activity_key: str) -> bool:
        claim = getattr(self.graph, "try_claim_activity", None)
        if not callable(claim):
            return True
        try:
            return bool(
                claim(
                    worker=self.worker_id,
                    key=activity_key,
                    lease_s=max(30.0, float(self.timeout_seconds) + 15.0),
                )
            )
        except Exception:
            return True

    def _release_endpoint_activity(self, activity_key: str, claimed: bool) -> None:
        if not claimed:
            return
        release = getattr(self.graph, "release_activity", None)
        if not callable(release):
            return
        try:
            release(worker=self.worker_id, key=activity_key)
        except Exception:
            pass

    def _load_visited_endpoint_keys(self) -> set[str]:
        """Read a compact endpoint index from existing Blackboard facts."""

        visited: set[str] = set()
        def collect(items: Any) -> None:
            for item in items or ():
                if not isinstance(item, Mapping):
                    continue
                content = item.get("content") or item.get("fact") or ""
                try:
                    value = json.loads(str(content))
                except (TypeError, ValueError):
                    value = item
                if isinstance(value, Mapping):
                    stored_key = str(value.get("endpoint_key") or "").strip()
                    if stored_key:
                        visited.add(stored_key)
                        continue
                    endpoint = str(value.get("endpoint") or value.get("path") or "")
                    if endpoint:
                        visited.add(
                            self._endpoint_key(
                                value.get("tool") or "http_request",
                                value.get("method") or "GET",
                                endpoint,
                                {},
                                "default",
                            )
                        )

        # Production passes the native Muteki SQLiteSharedGraph directly to
        # this Worker.  Its authoritative read API is ``verified_evidence``;
        # the compatibility facade's ``snapshot`` is intentionally not part of
        # the native object.  Read both surfaces so route deduplication remains
        # a SharedGraph rule in either backend.
        verified_evidence = getattr(self.graph, "verified_evidence", None)
        if callable(verified_evidence):
            try:
                collect(verified_evidence())
            except Exception:
                pass
        snapshot_reader = getattr(self.graph, "snapshot", None)
        snapshot = snapshot_reader() if callable(snapshot_reader) else None
        if not isinstance(snapshot, Mapping):
            return visited
        collect(snapshot.get("facts", ()) or ())
        return visited

    @staticmethod
    def _endpoint_key(
        tool_name: object,
        method: object,
        url: object,
        params: object,
        session_name: object,
    ) -> str:
        """Normalize route identity without retaining query values or bodies."""

        parsed = urlsplit(str(url or ""))
        path = parsed.path or "/"
        names: list[str] = []
        if isinstance(params, Mapping):
            names.extend(str(key).strip().casefold() for key in params if str(key).strip())
        names.extend(str(key).casefold() for key, _ in parse_qsl(parsed.query, keep_blank_values=True))
        del tool_name, session_name
        return ":".join(
            (
                str(method or "GET").strip().upper(),
                path[:240],
                ",".join(sorted(set(names)))[:180],
            )
        )

    def _flag_matcher(self) -> re.Pattern[str]:
        """Return the challenge flag shape instead of a hardcoded banner.

        The official SharedGraph carries the challenge, so a challenge using a
        non-``flag`` banner (``CTF2{...}``) must be able to submit its own flag
        through this Worker as well.
        """

        challenge = getattr(self.graph, "_challenge", None)
        # ``_challenge`` is the *ORM* Challenge here, whose column is
        # ``flag_pattern``; ``flag_format`` is the upstream (muteki) spelling and
        # only exists on the projected model.  Reading just the upstream name made
        # this fall through to ``_FLAG_RE`` on every host-side graph, so an arena
        # task declaring ``CTF2{...}`` had its genuine flag rejected with
        # ``FLAG_FORMAT_INVALID`` -- the run ended with no candidate at all.
        pattern = str(
            getattr(challenge, "flag_format", "")
            or getattr(challenge, "flag_pattern", "")
            or ""
        ).strip()
        # ``token`` means "no shape"; compiling it would only accept the literal
        # word, which is strictly worse than the banner default.
        if not pattern or pattern.casefold() == "token":
            return _FLAG_RE
        try:
            return re.compile(pattern)
        except re.error:
            return _FLAG_RE

    def _submit_flag(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        candidate = str(arguments.get("flag") or "").strip()
        if not self._flag_matcher().fullmatch(candidate):
            return {
                "verified": False,
                "error": "FLAG_FORMAT_INVALID",
                "detail": (
                    f"{candidate[:80]!r} does not have the flag's shape. Submit the "
                    "exact string the target returned, including its wrapper."
                ),
            }
        if not any(candidate in str(record.get("body") or "") for record in self.records):
            # A guess that fails here is not a formatting problem, and the model
            # otherwise retries the guess instead of going back for the value --
            # observed on a target where the Worker proved code execution with a
            # marker and never used it to read the flag file.
            return {
                "verified": False,
                "error": "FLAG_NOT_IN_OBSERVED_RESPONSE",
                "detail": (
                    "That value never appeared in any response you received, so it "
                    "cannot be right. Do not guess: use the access you already have "
                    "to read the flag, then submit the value you actually saw."
                ),
            }
        self.verified_flag = candidate
        return {"verified": True}

    def _report_dead_end(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        reason = sanitize_dead_end_reason(arguments.get("reason"))
        if not reason:
            return {"accepted": False, "error": "DEAD_END_REASON_REQUIRED"}
        self.dead_end_signal = DeadEndSignal(
            kind=DeadEndKind.ROUTE_DEAD_END,
            reason=reason,
            route_hash=route_key(
                {"route_hash": arguments.get("route_hash")},
                intent_id=str(self.intent_id or ""),
            ),
        )
        return {"accepted": True, "kind": DeadEndKind.ROUTE_DEAD_END.value}

    def _write_safe_fact(
        self,
        tool_name: str,
        url: str,
        observed: Mapping[str, Any],
        *,
        method: str = "GET",
        params: Any = None,
        session_name: str = "default",
    ) -> dict[str, Any]:
        path = urlsplit(url).path or "/"
        fact = {
            "tool": tool_name,
            "success": bool(observed.get("success")),
            "status_code": int(observed.get("status_code") or 0),
            "endpoint": path[:200],
            "method": str(method or "GET")[:12],
            "endpoint_key": self._endpoint_key(tool_name, method, path, params, session_name),
            "request_count": self._request_count,
        }
        add_evidence = getattr(self.graph, "add_evidence", None)
        if callable(add_evidence):
            add_evidence(
                actor=self.worker_id,
                source=self.worker_id,
                fact=json.dumps(fact, ensure_ascii=False, sort_keys=True),
                verified=True,
                confidence=0.8,
                intent_id=self.intent_id,
            )
        return fact

    def _result(self, success: bool, reason: str, **extra: Any):
        artifact_path = ""
        if self.records:
            root = self.workspace / ".muteki-artifacts" / f"chat-{_safe_name(self.worker_id)}"
            root.mkdir(parents=True, exist_ok=True)
            path = root / "http-evidence.json"
            path.write_text(
                json.dumps(
                    {
                        "worker_id": self.worker_id,
                        "intent_id": self.intent_id,
                        "records": self.records,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            artifact_path = str(path)
        usage = self.cost.global_tokens()
        snapshot = self.cost.snapshot()
        metadata = {
            "model": self.model,
            "role": "worker",
            "input_tokens": int(usage.get("input_tokens", 0) or 0),
            "output_tokens": int(usage.get("output_tokens", 0) or 0),
            "cost_usd": float(snapshot.get("global_usd", 0.0) or 0.0),
            "calls": int(snapshot.get("calls", 0) or 0),
            "num_turns": int(snapshot.get("calls", 0) or 0),
            "request_count": self._request_count,
            "python_run_count": self._python_run_count,
            "result_code": reason,
            "route_exhausted": bool(self.route_exhausted),
            "duplicate_request_count": self._duplicate_request_count,
            **extra,
        }
        if self.verified_flag:
            metadata["verified_flag"] = self.verified_flag
        from app.solver.muteki.worker.official_worker import OfficialWorkerResult

        return OfficialWorkerResult(
            False if self.dead_end_signal is not None else success,
            "COMPLETED" if success else "FAILED",
            self.model,
            output=reason,
            metadata=metadata,
            evidence_artifact_path=artifact_path,
            dead_end_signal=self.dead_end_signal,
        )

    def _initial_prompt(self, goal: str, payload: Mapping[str, Any]) -> str:
        facts: list[str] = []
        intents: list[dict[str, str]] = []
        dead_ends: list[str] = []
        visited_endpoints = sorted(self._visited_endpoint_keys)[:40]
        snapshot = getattr(self.graph, "snapshot", lambda: None)()
        if isinstance(snapshot, Mapping):
            evidence_items = snapshot.get("facts") or snapshot.get("evidence") or ()
            for item in snapshot.get("intents", ()) or ():
                if isinstance(item, Mapping):
                    intents.append(
                        {
                            "goal": str(item.get("description") or "")[:260],
                            "status": str(item.get("status") or "")[:40],
                            "route_hash": str(item.get("route_hash") or "")[:120],
                        }
                    )
            dead_ends = [
                str(item.get("description") or "")[:200]
                for item in snapshot.get("dead_ends", ()) or ()
                if isinstance(item, Mapping) and str(item.get("description") or "").strip()
            ]
        else:
            evidence_items = getattr(snapshot, "evidence", ()) or ()
        for item in list(evidence_items)[-16:]:
            if isinstance(item, Mapping):
                text = str(item.get("content") or item.get("fact") or "")[:240]
                refs = item.get("evidence_refs")
                if isinstance(refs, (list, tuple)) and refs:
                    safe_refs = ",".join(str(ref)[:120] for ref in refs[:8] if str(ref))
                    if safe_refs:
                        text = f"{text} [evidence_refs:{safe_refs}]"
            else:
                text = str(getattr(item, "fact", "") or "")[:240]
            if text:
                facts.append(text)
        return json.dumps(
            {
                "target": self.target_url,
                "goal": goal or "advance the current route",
                # Placed before the graph summary on purpose: the payload is
                # truncated at 12k characters from the tail, and these are the
                # few lines that tell a Worker which parameter name the
                # application actually accepts.  Losing them to truncation was
                # the failure mode that made a correct technique hint useless.
                "challenge_declared_surface": self.challenge_brief,
                "route_instruction": (
                    f"Stay inside reconnaissance lane {payload.get('race_lane')}. "
                    "Do not duplicate other lanes."
                    if payload.get("race_lane") else ""
                ),
                "intent": self.intent_id,
                "payload": {str(key): str(value)[:400] for key, value in payload.items()},
                "shared_graph_facts": facts,
                "shared_graph_intents": intents[-20:],
                "shared_graph_dead_ends": dead_ends[-10:],
                "visited_endpoints": visited_endpoints,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )[:12000]

    def _target_url(self, value: str) -> str:
        candidate = value.strip()
        if candidate.startswith("/"):
            candidate = self.target_url.rstrip("/") + candidate
        parsed = urlsplit(candidate)
        if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").casefold() != self.target_host:
            return ""
        return candidate


async def probe_openai_compatible(
    *, base_url: str, api_key: str, model: str, wire_api: str = "chat_completions"
) -> tuple[bool, str]:
    """Perform one real model probe without touching the target."""

    client = LLMClient(
        api_key=api_key,
        base_url=base_url,
        timeout=20.0,
        overall_timeout=25.0,
        wire_api=wire_api,
    )
    try:
        response = await client.chat(
            model=model,
            messages=[{"role": "user", "content": "Reply exactly OK."}],
            max_tokens=8,
            stream=False,
        )
        return bool(response.content.strip()), ""
    except Exception as error:
        return False, type(error).__name__
    finally:
        await client.aclose()


def _command_preview(tool: str, arguments: Mapping[str, Any]) -> str:
    """Render one tool invocation as a single auditable line.

    The interesting field differs per tool (a shell command vs a python body vs
    an HTTP url), so the preview names the tool and then prefers the field a
    human would actually want to replay.  Anything unexpected falls back to the
    whole argument object -- never to an empty string, because a blank command
    in the trace is indistinguishable from a tool that was never called.
    """

    for key in ("command", "cmd", "code", "url", "query", "sql"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            if key == "url":
                method = str(arguments.get("method") or "GET").upper()
                return f"{tool}: {method} {value}"
            return f"{tool}: {value}"
    try:
        return f"{tool}: {json.dumps(dict(arguments), ensure_ascii=False)}"
    except Exception:
        return f"{tool}: <unserialisable arguments>"


def _result_preview(result: Any) -> str:
    """Serialise a tool result for the trace, bounded by the caller's limit."""

    try:
        return json.dumps(result, ensure_ascii=False)
    except Exception:
        return str(result)


def _parse_tool_arguments(raw: str) -> tuple[dict[str, Any], str]:
    """Parse a tool call's arguments, recovering the common model mistake.

    ``ToolCall.parsed_args`` swallows a JSON error and returns ``{}``, so a
    model that emits a multi-line script with literal newlines inside the string
    silently loses its own payload and then retries the same broken shape.
    ``json.loads(..., strict=False)`` accepts raw control characters inside
    strings, which is exactly that failure.  When even that fails we return an
    explicit code instead of pretending the call had no arguments.
    """

    text = str(raw or "").strip()
    if not text:
        return {}, ""
    try:
        parsed = json.loads(text)
    except ValueError:
        try:
            parsed = json.loads(text, strict=False)
        except ValueError:
            return {}, "TOOL_ARGUMENTS_NOT_JSON"
    if not isinstance(parsed, Mapping):
        return {}, "TOOL_ARGUMENTS_NOT_OBJECT"
    return dict(parsed), ""


def _safe_mapping(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key)[:80]: str(item)[:500] for key, item in value.items()}


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", str(value or "worker"))[:80]


def _parse_trailing_json(text: str) -> Any:
    """Return the last JSON object printed on ``text``, or ``None``.

    The wrapper prints exactly one JSON line, but a shell/login profile or a
    partial write can prepend noise, so scanning backwards is safer than
    ``json.loads`` over the whole buffer.
    """

    for line in reversed(str(text or "").splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, Mapping):
            return parsed
    return None


_HTTP_SCRIPT = r'''
import base64, json, os, sys, urllib.error, urllib.parse, urllib.request

cfg = json.loads(base64.b64decode(sys.argv[1]).decode())
root = cfg["session_path"]
os.makedirs(root, exist_ok=True)
session = cfg.get("session_name") or "default"
jar_path = os.path.join(root, session + ".json")
try:
    with open(jar_path, encoding="utf-8") as handle:
        jar = json.load(handle)
except Exception:
    jar = {}
url = cfg["url"]
params = cfg.get("params") or {}
if params:
    query = urllib.parse.urlencode(params, doseq=True)
    url = url + ("&" if "?" in url else "?") + query
headers = {str(k): str(v) for k, v in (cfg.get("headers") or {}).items()}
if jar:
    headers["Cookie"] = "; ".join(k + "=" + v for k, v in jar.items())
body = str(cfg.get("body") or "").encode()
request = urllib.request.Request(url, data=body or None, headers=headers, method=str(cfg.get("method") or "GET"))
try:
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read(65536)
        status = int(response.status)
        final_url = response.geturl()
        response_headers = {"content-type": response.headers.get("content-type", ""), "location": response.headers.get("location", "")}
        set_cookie = response.headers.get_all("set-cookie") or []
        for item in set_cookie:
            pair = item.split(";", 1)[0]
            if "=" in pair:
                key, value = pair.split("=", 1)
                jar[key.strip()] = value.strip()
except urllib.error.HTTPError as error:
    raw = error.read(65536)
    status = int(error.code)
    final_url = error.geturl()
    response_headers = {"content-type": error.headers.get("content-type", ""), "location": error.headers.get("location", "")}
except Exception as error:
    print(json.dumps({"success": False, "error": type(error).__name__}))
    raise SystemExit(0)
try:
    with open(jar_path, "w", encoding="utf-8") as handle:
        json.dump(jar, handle)
except Exception:
    pass
text = raw.decode("utf-8", "replace")
print(json.dumps({"success": True, "status_code": status, "final_url": final_url, "headers": response_headers, "cookie_names": sorted(jar), "body": text}, ensure_ascii=False))
'''

_PYTHON_RUN_SCRIPT = r'''
import base64, json, os, subprocess, sys, tempfile, time

cfg = json.loads(base64.b64decode(sys.argv[1]).decode())
code = str(cfg.get("code") or "")
# Inherit the cwd the executor already resolved inside the container.
cwd = str(cfg.get("cwd") or "") or os.getcwd()
timeout = max(5.0, float(cfg.get("timeout") or 60))
out_limit = 12000

fd, path = tempfile.mkstemp(
    prefix="muteki-run-", suffix=".py",
    dir="/tmp" if os.path.isdir("/tmp") else None,
)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    handle.write(code)

env = dict(os.environ)
env["PYTHONUNBUFFERED"] = "1"
env["PYTHONIOENCODING"] = "utf-8"
env["MUTEKI_TARGET"] = str(cfg.get("target") or "")

def _preexec():
    # A runaway print loop must not fill the container's disk.  On other
    # platforms (host-side unit tests) the hook is simply not installed.
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024 * 1024, 2 * 1024 * 1024))
    except Exception:
        pass

started = time.time()
stdout_path = path + ".out"
stderr_path = path + ".err"
timed_out = False
exit_code = None
error = ""
detail = ""
try:
    with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
        kwargs = {"cwd": cwd, "env": env, "stdin": subprocess.DEVNULL,
                  "stdout": out, "stderr": err}
        if os.name == "posix":
            kwargs["preexec_fn"] = _preexec
        proc = subprocess.Popen([sys.executable or "python3", "-u", path], **kwargs)
        try:
            exit_code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                proc.kill()
            except Exception:
                pass
            try:
                exit_code = proc.wait(timeout=5)
            except Exception:
                exit_code = -9
except Exception as exc:
    error = type(exc).__name__
    detail = str(exc)[:300]

def _read(target, limit):
    try:
        with open(target, "rb") as handle:
            return handle.read(limit * 4).decode("utf-8", "replace")[:limit]
    except Exception:
        return ""

stdout = _read(stdout_path, out_limit)
stderr = _read(stderr_path, out_limit)
for leftover in (path, stdout_path, stderr_path):
    try:
        os.unlink(leftover)
    except Exception:
        pass

print(json.dumps({
    "success": (not timed_out) and (not error) and exit_code == 0,
    "exit_code": exit_code,
    "timed_out": timed_out,
    "duration_seconds": round(time.time() - started, 2),
    "cwd": cwd,
    "stdout": stdout,
    "stderr": stderr,
    "error": error,
    "detail": detail,
}, ensure_ascii=False))
'''

__all__ = [
    "HTTP_TOOLS",
    "WORKER_TOOLS",
    "OpenAICompatibleWorker",
    "probe_openai_compatible",
]
