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
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from muteki.core.cost import CostController
from muteki.core.llm import LLMClient
from muteki.solver.cli_driver import CliDriver, CliResult
from muteki.solver.container_exec import run_cli_streaming_container

from ..outcomes import DeadEndKind, DeadEndSignal, route_key, sanitize_dead_end_reason

_SESSION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_FLAG_RE = re.compile(r"^flag\{[^}\r\n]{1,200}\}$")
_SAFE_HEADERS = frozenset({"accept", "content-type", "user-agent"})


HTTP_TOOLS: tuple[dict[str, Any], ...] = (
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
            "name": "submit_flag",
            "description": "Submit a flag only when the exact value was observed in a real HTTP response.",
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
            "name": "report_dead_end",
            "description": "Explicitly rule out the current route after real target-side evidence. Do not use for provider, timeout, or authentication failures.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "route_hash": {"type": "string"},
                },
                "required": ["reason"],
                "additionalProperties": False,
            },
        },
    },
)


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
    ) -> None:
        self.graph = graph
        self.container = container
        self.workspace = Path(workspace).resolve()
        self.worker_id = worker_id
        self.intent_id = intent_id
        self.target_url = target_url.rstrip("/") + "/"
        self.target_host = (urlsplit(target_url).hostname or "").casefold()
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_turns = max(1, min(int(max_turns), 24))
        self.timeout_seconds = max(5, min(int(timeout_seconds), 120))
        self.cost = cost
        self.run_id = run_id
        self.records: list[dict[str, Any]] = []
        self.verified_flag: str | None = None
        self.dead_end_signal: DeadEndSignal | None = None
        self._request_count = 0
        self._new_endpoint_count = 0
        self._visited_endpoint_keys = self._load_visited_endpoint_keys()
        self._duplicate_request_count = 0
        self._consecutive_duplicate_requests = 0
        self.route_exhausted = False
        self._cancel_event = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[Any] | None = None

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
                    "real HTTP response. Continue exploring after a login page; do not "
                    "stop after one request. Never repeat a visited endpoint; if the "
                    "assigned route has no new endpoint, report ROUTE_EXHAUSTED."
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
        )
        try:
            for _ in range(self.max_turns):
                if self._cancelled():
                    return self._result(False, "INTERRUPTED")
                response = await client.chat(
                    model=self.model,
                    messages=messages,
                    tools=list(HTTP_TOOLS),
                    temperature=0.0,
                    max_tokens=4000,
                    stream=False,
                    run_id=self.run_id,
                    challenge_id=self.run_id,
                    solver_id=self.worker_id,
                )
                if not response.tool_calls:
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
                    result = await self._dispatch_tool(call.name, call.parsed_args())
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
            return self._result(
                False,
                "OPENAI_COMPATIBLE_WORKER_FAILED",
                error_type=type(error).__name__,
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
            return self._result(False, "OPENAI_COMPATIBLE_NO_TOOL_CALL")
        return self._result(True, "COMPLETED")

    async def _dispatch_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name in {"http_request", "http_session_request"}:
            return await self._http(name, arguments)
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
                "route_exhausted": True,
                "endpoint": endpoint_key,
            }
        session_name = str(arguments.get("session_name") or "default")
        if not _SESSION_RE.fullmatch(session_name):
            return {"success": False, "error": "INVALID_SESSION_NAME"}
        self._request_count += 1
        if self._request_count > 16:
            return {"success": False, "error": "REQUEST_BUDGET_EXHAUSTED"}
        activity_key = f"http:{endpoint_key}"
        activity_claimed = self._claim_endpoint_activity(activity_key)
        if not activity_claimed:
            self._request_count -= 1
            self._duplicate_request_count += 1
            self._consecutive_duplicate_requests += 1
            self.route_exhausted = True
            return {
                "success": False,
                "error": "ROUTE_EXHAUSTED",
                "route_exhausted": True,
                "endpoint": endpoint_key,
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
                "route_exhausted": True,
                "endpoint": endpoint_key,
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

    def _submit_flag(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        candidate = str(arguments.get("flag") or "").strip()
        if not _FLAG_RE.fullmatch(candidate):
            return {"verified": False, "error": "FLAG_FORMAT_INVALID"}
        if not any(candidate in str(record.get("body") or "") for record in self.records):
            return {"verified": False, "error": "FLAG_NOT_IN_OBSERVED_RESPONSE"}
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


async def probe_openai_compatible(*, base_url: str, api_key: str, model: str) -> tuple[bool, str]:
    """Perform one real Chat Completions probe without touching the target."""

    client = LLMClient(
        api_key=api_key,
        base_url=base_url,
        timeout=20.0,
        overall_timeout=25.0,
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


def _safe_mapping(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key)[:80]: str(item)[:500] for key, item in value.items()}


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", str(value or "worker"))[:80]


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

__all__ = ["HTTP_TOOLS", "OpenAICompatibleWorker", "probe_openai_compatible"]
