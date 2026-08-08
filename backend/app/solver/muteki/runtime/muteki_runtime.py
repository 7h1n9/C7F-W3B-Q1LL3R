from __future__ import annotations

from pathlib import Path
from typing import Any

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
from app.solver.muteki.graph import MutekiGraph
from app.solver.muteki.reason import MutekiReason
from app.solver.muteki.workers import EngineProfile, WorkerJob, WorkerOutcome


class MutekiRuntime:
    """Build one isolated canonical runtime for an existing SolveRun."""

    def __init__(self, session: Any, run: SolveRun, challenge: Challenge) -> None:
        self.session = session
        self.run = run
        self.challenge = challenge
        self.tool_adapter = ToolAdapter(session, run, challenge)
        self.runner_adapter = RunnerAdapter()
        self.evidence_adapter = EvidenceAdapter()
        self.event_bridge: EventBridge | None = None
        self._graph: MutekiGraph | None = None

    async def run_once(self, *, max_rounds: int = 10, max_workers: int = 10) -> MutekiRunResult:
        root = Path(self.run.workspace_path).resolve() / "muteki"
        graph_path = root / "graph" / "shared_graph.db"
        # Event persistence uses a short-lived independent session because
        # graph callbacks can be scheduled while the worker session is busy
        # collecting ToolGateway results.
        self.event_bridge = EventBridge(SessionLocal, run_id=self.run.id)
        self._graph = MutekiGraph(graph_path, challenge_id=self.run.id, event_subscriber=self.event_bridge.callback())
        reason = MutekiReason(provider=self._reason_provider)
        orchestrator = MutekiOrchestrator(
            self._graph,
            reason,
            worker_runner=self._worker_runner,
            engines=[EngineProfile("gateway-runner")],
            max_workers=max_workers,
        )
        try:
            result = await orchestrator.run(max_rounds=max_rounds)
            await self.event_bridge.flush()
            return result
        finally:
            self._graph.close()

    def _reason_provider(self, snapshot: dict) -> list[dict[str, Any]]:
        if snapshot.get("facts"):
            return []
        target = str(self.challenge.target_url or "")
        if not target:
            return [{"goal": "target URL is not configured", "payload": {}}]
        return [{
            "goal": "establish target HTTP baseline",
            "worker_class": "gateway",
            "rationale": "Start with one bounded request through the existing Tool Gateway.",
            "payload": {"tool_name": "http_request", "arguments": {"method": "GET", "url": target}},
        }]

    async def _worker_runner(self, job: WorkerJob) -> WorkerOutcome:
        graph = self._graph
        if graph is None:
            return WorkerOutcome(job.worker_id, "FAILED", result="GRAPH_NOT_INITIALIZED")
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
        fact = self.tool_adapter.to_fact(result, source_worker_id=job.worker_id)
        evidence_ref = await self.evidence_adapter.write_fact(fact, self.run.id, job.worker_id)
        refs = list(result.evidence_refs)
        if evidence_ref and evidence_ref not in refs:
            refs.append(evidence_ref)
        graph.add_fact(actor=job.worker_id, content=fact.content, verified=fact.verified, evidence_refs=refs, dedupe_key=f"{job.intent_id or tool_name}:{result.tool_call_id or ''}")
        candidate = result.output.get("flag") or result.output.get("extracted_value") or result.output.get("answer")
        real_output = result.output.get("real_output") or result.output.get("summary") or ""
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
