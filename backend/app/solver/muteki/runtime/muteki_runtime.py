from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from app.core.database import SessionLocal
from app.models.challenge import Challenge
from app.models.run import SolveRun
from app.solver.muteki.adapter import (
    EventBridge,
    EvidenceAdapter,
    RunnerAdapter,
    ToolAdapter,
    ToolResult,
)
from app.solver.muteki.core.orchestrator import MutekiOrchestrator, MutekiRunResult
from app.solver.muteki.core.race import RaceWorker
from app.solver.muteki.graph import MutekiGraph
from app.solver.muteki.reason import MutekiReason
from app.solver.muteki.recon.fingerprint import classify_challenge
from app.solver.muteki.workers import EngineProfile, WorkerJob, WorkerOutcome


@dataclass(frozen=True, slots=True)
class _ChallengeSnapshot:
    """Immutable challenge data used after ToolGateway commits the session."""

    id: str
    name: str
    description: str
    challenge_type: str
    target_url: str | None
    allowed_hosts: list[str]
    flag_pattern: str
    source_path: str | None
    metadata_json: dict[str, Any]


class MutekiRuntime:
    """Build one isolated canonical runtime for an existing SolveRun."""

    def __init__(self, session: Any, run: SolveRun, challenge: Challenge) -> None:
        self.session = session
        self.run = run
        # Gateway commits happen between Solver turns.  Keep Runtime reads
        # detached from the SQLAlchemy Challenge instance so the next Reason
        # pass cannot trigger an async lazy load (MissingGreenlet).
        self.challenge = _ChallengeSnapshot(
            id=str(challenge.id),
            name=str(challenge.name or ""),
            description=str(challenge.description or ""),
            challenge_type=str(challenge.challenge_type or "WEB_TARGET"),
            target_url=str(challenge.target_url) if challenge.target_url else None,
            allowed_hosts=[str(item) for item in (challenge.allowed_hosts or [])],
            flag_pattern=str(challenge.flag_pattern or r"flag\{[^}]+\}"),
            source_path=str(challenge.source_path) if challenge.source_path else None,
            metadata_json=dict(challenge.metadata_json or {}),
        )
        self.tool_adapter = ToolAdapter(session, run, self.challenge)
        self.runner_adapter = RunnerAdapter()
        self.evidence_adapter = EvidenceAdapter()
        self.event_bridge: EventBridge | None = None
        self._graph: MutekiGraph | None = None
        self._public_credentials: tuple[str, str] | None = None

    async def run_once(self, *, max_rounds: int = 10, max_workers: int = 10) -> MutekiRunResult:
        root = Path(self.run.workspace_path).resolve() / "muteki"
        graph_path = root / "graph" / "shared_graph.db"
        # Event persistence uses a short-lived independent session because
        # graph callbacks can be scheduled while the worker session is busy
        # collecting ToolGateway results.
        self.event_bridge = EventBridge(SessionLocal, run_id=self.run.id)
        self._graph = MutekiGraph(graph_path, challenge_id=self.run.id, event_subscriber=self.event_bridge.callback())
        reason = MutekiReason(provider=self._reason_provider, metadata=self.challenge.metadata_json or {})
        orchestrator = MutekiOrchestrator(
            self._graph,
            reason,
            worker_runner=self._worker_runner,
            engines=[EngineProfile("gateway-runner")],
            max_workers=max_workers,
        )
        try:
            result = await orchestrator.run(max_rounds=max_rounds)
            import asyncio

            try:
                await asyncio.wait_for(self.event_bridge.flush(), timeout=15.0)
            except asyncio.TimeoutError:
                self._graph.add_dead_end(actor="muteki-runtime", description="EVENT_BRIDGE_FLUSH_TIMEOUT")
            return result
        finally:
            self._graph.close()

    def _reason_provider(self, snapshot: dict) -> list[dict[str, Any]]:
        metadata = self.challenge.metadata_json or {}
        classification = classify_challenge(metadata, snapshot.get("facts", ()))
        if classification and classification.classification == "SQLI":
            return [{
                "goal": "sql_boolean_compare",
                "worker_class": "exploit",
                "rationale": "Challenge metadata or Race evidence identifies a SQL injection path.",
                "payload": {"tool_name": "sql_boolean_compare", "arguments": self._sql_boolean_arguments(metadata)},
            }]
        if classification and classification.classification == "IDOR":
            facts = "\n".join(
                str(item.get("content") or "")
                for item in snapshot.get("facts", ())
                if isinstance(item, dict)
            )
            folded = facts.casefold()
            target = str(self.challenge.target_url or "").rstrip("/")
            session_name = "muteki-recon"
            if "request_method=post" not in folded or "/login" not in folded:
                username, password = self._public_credentials or ("demo", "demo-pass")
                return [{
                    "goal": "authenticate using the public demo form",
                    "worker_class": "exploit",
                    "rationale": "The public homepage exposed a bounded demo login form; reuse the existing recon session.",
                    "payload": {"tool_name": "http_session_request", "arguments": {
                        "session_name": session_name,
                        "method": "POST",
                        "url": f"{target}/login",
                        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
                        "body": f"username={username}&password={password}",
                        "follow_redirects": False,
                    }},
                }]
            if f"request_url={target}/tickets" not in folded:
                return [{
                    "goal": "open the authenticated ticket collection",
                    "worker_class": "exploit",
                    "rationale": "The same session is authenticated; enumerate the authorized ticket list.",
                    "payload": {"tool_name": "http_session_request", "arguments": {"session_name": session_name, "method": "GET", "url": f"{target}/tickets", "follow_redirects": False}},
                }]
            ticket_match = re.search(r"/tickets/(WO-[A-Za-z0-9-]+)", facts, re.IGNORECASE)
            if ticket_match and "/api/tickets/" not in folded:
                ticket = ticket_match.group(1)
                return [{
                    "goal": "read the discovered ticket API object",
                    "worker_class": "exploit",
                    "rationale": "The authenticated ticket page disclosed an object reference; validate the same-object API response.",
                    "payload": {"tool_name": "http_session_request", "arguments": {"session_name": session_name, "method": "GET", "url": f"{target}/api/tickets/{ticket}", "follow_redirects": False}},
                }]
            # After reading the caller's own object, probe a small, explicit
            # set of adjacent object IDs.  This is the bounded IDOR branch:
            # it is driven by the observed ticket number and never guesses a
            # challenge answer or reads challenge metadata.
            if "/api/tickets/" in folded and "download_url" not in folded:
                observed_ticket = re.findall(r"/api/tickets/(WO-[A-Za-z0-9-]+)", facts, re.IGNORECASE)
                candidate = _next_idor_ticket(observed_ticket, facts)
                if candidate:
                    return [{
                        "goal": f"test adjacent ticket object {candidate}",
                        "worker_class": "exploit",
                        "rationale": "The authenticated API exposed an object identifier; test a bounded adjacent identifier for authorization isolation.",
                        "payload": {"tool_name": "http_session_request", "arguments": {"session_name": session_name, "method": "GET", "url": f"{target}/api/tickets/{candidate}", "follow_redirects": False}},
                    }]
            report_match = re.search(r"download_url\s*[\"']?\s*[:=]\s*[\"']?([^\"'\s,}]+)", facts, re.IGNORECASE)
            if report_match:
                report_url = urljoin(target + "/", report_match.group(1))
                return [{
                    "goal": "retrieve the referenced diagnostic report",
                    "worker_class": "exploit",
                    "rationale": "The ticket API returned an evidence-backed diagnostic report reference.",
                    "payload": {"tool_name": "http_session_request", "arguments": {"session_name": session_name, "method": "GET", "url": report_url, "follow_redirects": False}},
                }]
        if snapshot.get("facts"):
            return []
        target = str(self.challenge.target_url or "")
        if not target:
            return []
        return [{
            "goal": "establish target HTTP baseline",
            "worker_class": "gateway",
            "rationale": "Start with one bounded request through the existing Tool Gateway.",
            "payload": {"tool_name": "http_request", "arguments": {"method": "GET", "url": target}},
        }]

    def _sql_boolean_arguments(self, metadata: dict[str, Any]) -> dict[str, Any]:
        request_metadata = metadata.get("request") if isinstance(metadata.get("request"), dict) else {}
        endpoint = str(metadata.get("endpoint") or request_metadata.get("url") or request_metadata.get("path") or "/")
        target = str(self.challenge.target_url or "")
        url = endpoint if endpoint.startswith(("http://", "https://")) else urljoin(target.rstrip("/") + "/", endpoint.lstrip("/"))
        fields = metadata.get("fields") if isinstance(metadata.get("fields"), list) else request_metadata.get("fields")
        fields = [str(item) for item in (fields or ["query"]) if str(item)]
        controls = metadata.get("control_values") if isinstance(metadata.get("control_values"), dict) else request_metadata.get("control_values")
        controls = dict(controls or {fields[0]: "test"})
        request = dict(request_metadata)
        request.setdefault("method", str(metadata.get("method") or "GET"))
        request["url"] = url
        request.setdefault("params", controls)
        return {
            "request": request,
            "test_field": fields[0],
            "control_fields": fields,
            "baseline_value": controls.get(fields[0], "test"),
            "max_requests": 5,
        }

    async def _worker_runner(self, job: WorkerJob) -> WorkerOutcome:
        graph = self._graph
        if graph is None:
            return WorkerOutcome(job.worker_id, "FAILED", result="GRAPH_NOT_INITIALIZED")
        # Graph callbacks are scheduled asynchronously.  Drain the events
        # emitted by the Coordinator/previous worker before entering the
        # ToolGateway, so durable RunEvent writes cannot overlap the gateway's
        # ToolCall transaction for the same Run.
        if self.event_bridge is not None:
            import asyncio

            try:
                await asyncio.wait_for(self.event_bridge.flush(), timeout=15.0)
            except asyncio.TimeoutError:
                graph.add_dead_end(actor="muteki-runtime", description="EVENT_BRIDGE_PRE_TOOL_FLUSH_TIMEOUT")
        if job.role == "race":
            race_worker = RaceWorker(
                graph,
                self.tool_adapter.execute_tool,
                target_url=str(self.challenge.target_url or ""),
                metadata=self.challenge.metadata_json or {},
                workspace_id=str(self.run.workspace_path),
                run_id=str(self.run.id),
            )
            race_result = await race_worker.run(worker_id=job.worker_id)
            self._public_credentials = race_worker.public_credentials
            return WorkerOutcome(job.worker_id, "COMPLETED", flag_found=race_result.flag_found, result=race_result.classification.classification)
        payload = dict(job.payload or {})
        tool_name = str(payload.get("tool_name") or "")
        arguments = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
        if not tool_name and job.role == "race" and self.challenge.target_url:
            tool_name = "http_request"
            arguments = {"method": "GET", "url": str(self.challenge.target_url)}
        if not tool_name:
            graph.add_dead_end(actor=job.worker_id, description="intent has no tool_name")
            if job.intent_id:
                graph.conclude_intent(actor=job.worker_id, intent_id=job.intent_id, result="NO_TOOL")
            return WorkerOutcome(job.worker_id, "FAILED", result="NO_TOOL")
        if str(payload.get("backend") or "gateway") == "runner" and tool_name in {"python_run", "script_run"}:
            if tool_name == "python_run":
                runner_result = await self.runner_adapter.run_python(
                    str(arguments.get("code") or ""),
                    str(self.run.workspace_path),
                    self.run.id,
                    int(arguments.get("timeout_seconds") or 60),
                )
            else:
                runner_result = await self.runner_adapter.run_script(
                    str(arguments.get("path") or ""),
                    list(arguments.get("args") or []),
                    str(self.run.workspace_path),
                    self.run.id,
                    int(arguments.get("timeout_seconds") or 60),
                )
            result = ToolResult(runner_result.success, tool_name, runner_result.output, error_code=runner_result.error_code)
        else:
            result = await self.tool_adapter.execute_tool(tool_name, arguments, str(self.run.workspace_path), self.run.id)
        fact = self.tool_adapter.to_fact(result, source_worker_id=job.worker_id, request=arguments)
        evidence_ref = await self.evidence_adapter.write_fact(fact, self.run.id, job.worker_id)
        refs = list(result.evidence_refs)
        if evidence_ref and evidence_ref not in refs:
            refs.append(evidence_ref)
        graph.add_fact(actor=job.worker_id, content=fact.content, verified=fact.verified, evidence_refs=refs, dedupe_key=f"{job.intent_id or tool_name}:{result.tool_call_id or ''}")
        candidate = result.output.get("flag") or result.output.get("extracted_value") or result.output.get("answer")
        real_output = result.output.get("real_output") or result.output.get("body_excerpt") or result.output.get("summary") or ""
        if not candidate:
            match = re.search(r"flag\{[^{}\r\n]+\}", str(real_output), re.IGNORECASE)
            candidate = match.group(0) if match else None
        flag_found = False
        if isinstance(candidate, str) and candidate.startswith("flag{"):
            graph.write_flag(actor=job.worker_id, flag=candidate, real_output=str(real_output))
            flag_found = bool(graph.flags(verified_only=True))
        if not result.success:
            graph.add_dead_end(actor=job.worker_id, description=f"{tool_name} failed: {result.error_code or 'TOOL_FAILED'}")
        if job.intent_id:
            graph.conclude_intent(actor=job.worker_id, intent_id=job.intent_id, result="SUCCESS" if result.success else "FAILED")
        return WorkerOutcome(job.worker_id, "COMPLETED" if result.success else "FAILED", flag_found=flag_found, result=result.output.get("summary", result.error_code or ""))


__all__ = ["MutekiRuntime"]


def _next_idor_ticket(observed_api_tickets: list[str], facts: str) -> str | None:
    """Return the next bounded numeric ticket candidate not yet requested."""

    if not observed_api_tickets:
        return None
    match = re.match(r"^(.*?)(\d+)$", observed_api_tickets[-1])
    if not match:
        return None
    prefix, number = match.groups()
    base = int(number)
    requested = {item.casefold() for item in re.findall(r"request_url=([^;\s]+/api/tickets/[^;\s]+)", facts, re.IGNORECASE)}
    for offset in (1, 2, 3, 4, 5, -1, -2):
        candidate = f"{prefix}{base + offset:0{len(number)}d}"
        if f"/api/tickets/{candidate}".casefold() not in requested:
            return candidate
    return None
