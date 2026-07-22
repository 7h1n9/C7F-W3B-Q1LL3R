import contextlib
import hashlib
import json
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.challenge import Challenge
from app.models.run import (
    Artifact,
    LogicalToolCall,
    Observation,
    RunEvent,
    SolveRun,
    ToolCall,
    ToolExecutionTrace,
)
from app.orchestration.state_machine import TERMINAL, RunStatus
from app.services.flags import flag_service
from app.services.reports import report_service
from app.services.run_diagnostics import run_diagnostics_service
from app.services.runner_client import runner_client


class CodexMaterializer:
    _FORBIDDEN_DIRECT_TOOLS = {
        "command_execution",
        "node_repl",
        "node_repl.js",
        "web_search",
        "shell",
        "powershell",
        "cmd.exe",
        "bash",
    }

    async def sync(self, session: AsyncSession, run: SolveRun) -> None:
        if run.engine_type != "codex_sdk":
            return
        challenge = await session.get(Challenge, run.challenge_id)
        if not challenge:
            return
        events = list(
            (
                await session.scalars(
                    select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.sequence)
                )
            ).all()
        )
        verified_sequence = next(
            (event.sequence for event in events if event.event_type == "flag.verified"), None
        )
        if verified_sequence is not None:
            run.terminal_event_sequence = verified_sequence
            run.thread_invalidated = True
        for event in events:
            if run.thread_invalidated and run.terminal_event_sequence is not None and event.sequence > run.terminal_event_sequence and event.event_type in {
                "tool.requested", "tool.started", "tool.completed", "tool.failed", "artifact.created", "observation.created",
            }:
                existing = list(run.post_terminal_events_json or [])
                existing.append({"sequence": event.sequence, "event_type": event.event_type, "payload": event.payload_json or {}})
                run.post_terminal_events_json = existing[-200:]
                continue
            await self._apply_event(session, run, challenge, event)

        self._refresh_run_metrics(run, events)
        await session.commit()

        if RunStatus(run.status) in {RunStatus.COMPLETED_SOLVED, RunStatus.COMPLETED_UNSOLVED}:
            report = await session.scalar(select(Artifact).where(Artifact.run_id == run.id, Artifact.artifact_type == "report", Artifact.status == "ACTIVE"))
            if report is None:
                await report_service.generate(
                    session,
                    run,
                    challenge,
                    "solved" if RunStatus(run.status) == RunStatus.COMPLETED_SOLVED else "unsolved",
                    run.last_error_message or "",
                )
        elif RunStatus(run.status) in TERMINAL or RunStatus(run.status) == RunStatus.WAITING_CONFIGURATION:
            await run_diagnostics_service.write_artifact(session, run)
            with contextlib.suppress(Exception):
                await runner_client.clear_sessions(run.id)

    async def _apply_event(
        self, session: AsyncSession, run: SolveRun, challenge: Challenge, event: RunEvent
    ) -> None:
        if event.event_type in {"tool.started", "tool.completed", "tool.failed"}:
            await self._materialize_tool_event(session, run, challenge, event)
        elif event.event_type == "artifact.created":
            await self._materialize_artifact_event(session, run, event)

    async def _materialize_tool_event(
        self, session: AsyncSession, run: SolveRun, challenge: Challenge, event: RunEvent
    ) -> None:
        payload = event.payload_json or {}
        tool_call_ref = payload.get("tool_call_id")
        tool_name = payload.get("tool")
        if not tool_call_ref or not tool_name:
            return
        normalized_tool = str(tool_name)
        if (
            normalized_tool in self._FORBIDDEN_DIRECT_TOOLS
            or payload.get("error_code") == "CODEX_DIRECT_TOOL_FORBIDDEN"
        ):
            # Policy violations are audit evidence, not successful CTF tool
            # evidence.  Keeping them out of ToolCall/Artifact/Observation
            # prevents reports and learned-skill candidates from treating
            # forbidden host-side execution as a valid solving step.
            return
        marker = f"codex:{tool_call_ref}"
        logical_id = str(payload.get("logical_tool_call_id") or marker)
        tool_call = await session.scalar(select(ToolCall).where(ToolCall.run_id == run.id, ToolCall.logical_tool_call_id == logical_id))
        if tool_call is None:
            tool_call = ToolCall(
                run_id=run.id,
                tool_name=str(tool_name),
                arguments_json=self._tool_arguments(payload),
                status="STARTED" if event.event_type == "tool.started" else self._tool_status(event),
                runner_job_id=marker,
                started_at=event.created_at,
                finished_at=event.created_at if event.event_type != "tool.started" else None,
                logical_tool_call_id=logical_id,
                parent_tool_call_id=str(payload.get("parent_tool_call_id")) if payload.get("parent_tool_call_id") else None,
                execution_layer="codex_mcp",
            )
            session.add(tool_call)
            await session.flush()
            logical = LogicalToolCall(
                id=logical_id,
                run_id=run.id,
                attempt_id=str(payload.get("attempt_id")) if payload.get("attempt_id") else None,
                engine_type=run.engine_type,
                tool_name=str(tool_name),
                arguments_digest=hashlib.sha256(json.dumps(self._tool_arguments(payload), sort_keys=True, default=str).encode()).hexdigest(),
                status=tool_call.status,
                started_at=tool_call.started_at,
            )
            session.add(logical)
        logical = await session.get(LogicalToolCall, logical_id)
        if logical is None:
            logical = LogicalToolCall(
                id=logical_id,
                run_id=run.id,
                engine_type=run.engine_type,
                tool_name=str(tool_name),
                arguments_digest=hashlib.sha256(json.dumps(self._tool_arguments(payload), sort_keys=True, default=str).encode()).hexdigest(),
                status=tool_call.status,
                started_at=tool_call.started_at,
            )
            session.add(logical)
            await session.flush()
        if logical:
            logical.status = tool_call.status
            logical.finished_at = tool_call.finished_at
            payload_digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
            trace = await session.scalar(select(ToolExecutionTrace).where(
                ToolExecutionTrace.logical_tool_call_id == logical.id,
                ToolExecutionTrace.event_type == event.event_type,
                ToolExecutionTrace.external_id == tool_call.runner_job_id,
                ToolExecutionTrace.payload_digest == payload_digest,
            ))
            if trace is None:
                session.add(ToolExecutionTrace(
                    logical_tool_call_id=logical.id,
                    execution_layer=str(payload.get("execution_layer") or "codex_mcp"),
                    event_type=event.event_type,
                    external_id=tool_call.runner_job_id,
                    payload_digest=payload_digest,
                ))
        tool_call.tool_name = str(tool_name)
        tool_call.arguments_json = self._tool_arguments(payload)
        tool_call.status = "STARTED" if event.event_type == "tool.started" else self._tool_status(event)
        if tool_call.started_at is None:
            tool_call.started_at = event.created_at
        if event.event_type != "tool.started":
            tool_call.finished_at = event.created_at
        if logical:
            logical.status = tool_call.status
            logical.finished_at = tool_call.finished_at

        if event.event_type in {"tool.completed", "tool.failed"}:
            await self._materialize_tool_artifact(session, run, challenge, tool_call, event)

    async def _materialize_tool_artifact(
        self,
        session: AsyncSession,
        run: SolveRun,
        challenge: Challenge,
        tool_call: ToolCall,
        event: RunEvent,
    ) -> None:
        payload = event.payload_json or {}
        output = str(payload.get("output") or payload.get("result") or "")
        if not output:
            return
        root = Path(run.workspace_path).resolve()
        safe_marker = str(tool_call.runner_job_id or tool_call.id).replace(":", "_")
        relative = Path("responses") / "codex_sdk" / f"{safe_marker}.txt"
        target = (root / relative).resolve()
        if root not in target.parents:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.read_text(encoding="utf-8", errors="replace") != output:
            target.write_text(output, encoding="utf-8")
        content = target.read_text(encoding="utf-8", errors="replace")
        summary = self._summary(content, tool_call.tool_name, event.event_type)
        artifact = await session.scalar(
            select(Artifact).where(Artifact.run_id == run.id, Artifact.tool_call_id == tool_call.id)
        )
        if artifact is None:
            artifact = Artifact(
                run_id=run.id,
                tool_call_id=tool_call.id,
                artifact_type="tool_output",
                file_path=str(relative).replace("\\", "/"),
                mime_type="text/plain",
                size=target.stat().st_size,
                sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
                summary=summary,
            )
            session.add(artifact)
            await session.flush()
        else:
            artifact.file_path = str(relative).replace("\\", "/")
            artifact.mime_type = "text/plain"
            artifact.size = target.stat().st_size
            artifact.sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
            artifact.summary = summary
            await session.flush()

        observation = await session.scalar(
            select(Observation).where(Observation.tool_call_id == tool_call.id)
        )
        facts = {
            "tool": tool_call.tool_name,
            "ok": event.event_type == "tool.completed",
            "artifact_path": artifact.file_path,
            "exit_code": payload.get("exit_code"),
            "output_length": len(content),
            "truncated": False,
            "source": "codex_sdk",
            "tool_model_view": {
                "summary": summary,
                "content_excerpt": re.sub(r"(?i)(authorization|cookie|token|password)(\s*[:=]\s*)([^;\s,]+)", r"\1\2<redacted>", content[:8192]),
                "extracted_facts": {"tool": tool_call.tool_name, "exit_code": payload.get("exit_code"), "output_length": len(content)},
                "warnings": [],
                "suggested_next_dimensions": [],
            },
        }
        if observation is None:
            observation = Observation(
                run_id=run.id,
                tool_call_id=tool_call.id,
                artifact_id=artifact.id,
                observation_type="tool_result",
                summary=summary,
                facts_json=facts,
            )
            session.add(observation)
        else:
            observation.artifact_id = artifact.id
            observation.summary = summary
            observation.facts_json = facts
        await session.flush()
        await flag_service.extract_candidates(session, run, challenge, artifact, content)

    async def _materialize_artifact_event(
        self, session: AsyncSession, run: SolveRun, event: RunEvent
    ) -> None:
        payload = event.payload_json or {}
        changes = payload.get("changes") or []
        if not isinstance(changes, list):
            return
        root = Path(run.workspace_path).resolve()
        for change in changes:
            if not isinstance(change, dict):
                continue
            raw_path = change.get("path")
            if not raw_path:
                continue
            try:
                path = Path(raw_path).resolve()
            except Exception:
                continue
            if root not in path.parents and path != root:
                continue
            if not path.exists() or not path.is_file():
                continue
            relative = str(path.relative_to(root)).replace("\\", "/")
            artifact = await session.scalar(
                select(Artifact).where(Artifact.run_id == run.id, Artifact.file_path == relative)
            )
            content = path.read_bytes()
            summary = self._summary(path.read_text(encoding="utf-8", errors="replace"), relative, change.get("kind"))
            if artifact is None:
                artifact = Artifact(
                    run_id=run.id,
                    artifact_type=str(change.get("kind") or "workspace_change"),
                    file_path=relative,
                    mime_type=self._mime_type(path),
                    size=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    summary=summary,
                )
                session.add(artifact)
            else:
                artifact.artifact_type = str(change.get("kind") or artifact.artifact_type)
                artifact.mime_type = self._mime_type(path)
                artifact.size = len(content)
                artifact.sha256 = hashlib.sha256(content).hexdigest()
                artifact.summary = summary
            await session.flush()

    def _refresh_run_metrics(self, run: SolveRun, events: list[RunEvent]) -> None:
        seen_steps: set[str] = set()
        seen_tools: set[str] = set()
        for event in events:
            payload = event.payload_json or {}
            if event.event_type == "agent.turn_completed":
                seen_steps.add(str(event.sequence))
            tool_ref = payload.get("logical_tool_call_id") or payload.get("tool_call_id")
            if event.event_type.startswith("tool.") and isinstance(tool_ref, str) and tool_ref:
                seen_tools.add(tool_ref)
        run.agent_step_count = len(seen_steps)
        run.tool_call_count = len(seen_tools)
        run.run_total_agent_steps = len(seen_steps)
        run.run_total_logical_tool_calls = len(seen_tools)
        run.attempt_agent_steps = len(seen_steps)
        run.attempt_logical_tool_calls = len(seen_tools)
        run.checkpoint_segment_steps = len(seen_steps)

    @staticmethod
    def _tool_arguments(payload: dict) -> dict:
        command = payload.get("command")
        if command:
            return {"command": command}
        arguments = payload.get("arguments")
        return dict(arguments) if isinstance(arguments, dict) else {}

    @staticmethod
    def _tool_status(event: RunEvent) -> str:
        if event.event_type == "tool.completed":
            return "COMPLETED"
        if event.event_type == "tool.failed":
            return "FAILED"
        return "STARTED"

    @staticmethod
    def _summary(text: str, fallback: str, suffix: object) -> str:
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if first_line:
            return first_line[:240]
        return f"{fallback} ({suffix})"[:240]

    @staticmethod
    def _mime_type(path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix in {".md", ".txt", ".log", ".json", ".yaml", ".yml"}:
            return "text/plain"
        return "application/octet-stream"


codex_materializer = CodexMaterializer()
