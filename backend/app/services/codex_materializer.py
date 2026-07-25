import asyncio
import contextlib
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import func, select
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


def logical_tool_budget_ref(payload: dict) -> str | None:
    """Return one canonical budget key for a tool invocation.

    A Codex MCP call produces an outer ``ctfctl.*`` event and an inner
    Tool-Gateway event.  Only the latter carries ``logical_tool_call_id``;
    counting both makes one model action consume the budget twice.
    """
    logical_id = payload.get("logical_tool_call_id")
    if logical_id:
        return f"logical:{logical_id}"
    tool_name = str(payload.get("tool") or "")
    if tool_name.startswith("ctfctl."):
        return None
    tool_ref = payload.get("tool_call_id")
    return f"legacy:{tool_ref}" if tool_ref else None


@dataclass
class _MaterializationCursor:
    sequence: int = 0
    agent_steps: set[str] = field(default_factory=set)
    tool_calls: set[str] = field(default_factory=set)


class CodexMaterializer:
    def __init__(self) -> None:
        # The event stream and the workspace detail page can request
        # materialization at the same time. Serialize one run and remember the
        # last sequence so normal polling only handles newly appended events.
        self._locks: dict[str, asyncio.Lock] = {}
        self._cursors: dict[str, _MaterializationCursor] = {}

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
        lock = self._locks.setdefault(run.id, asyncio.Lock())
        async with lock:
            challenge = await session.get(Challenge, run.challenge_id)
            if not challenge:
                return

            latest_sequence = int(
                await session.scalar(
                    select(func.max(RunEvent.sequence)).where(RunEvent.run_id == run.id)
                )
                or 0
            )
            cursor = self._cursors.get(run.id)
            full_refresh = cursor is None or latest_sequence < cursor.sequence
            if full_refresh:
                events = list(
                    (
                        await session.scalars(
                            select(RunEvent)
                            .where(RunEvent.run_id == run.id)
                            .order_by(RunEvent.sequence)
                        )
                    ).all()
                )
                cursor = _MaterializationCursor()
            else:
                events = list(
                    (
                        await session.scalars(
                            select(RunEvent)
                            .where(
                                RunEvent.run_id == run.id,
                                RunEvent.sequence > cursor.sequence,
                            )
                            .order_by(RunEvent.sequence)
                        )
                    ).all()
                )
            if not events:
                cursor.sequence = latest_sequence
                self._cursors[run.id] = cursor
                return

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

            self._refresh_run_metrics(run, cursor, events)
            cursor.sequence = max(cursor.sequence, max(event.sequence for event in events))
            self._cursors[run.id] = cursor
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
        # Codex item ids are only unique inside one thread/run (for example
        # every run can contain ``item_1``).  The database primary key is
        # global, so using ``codex:item_1`` directly makes the second run fail
        # with a duplicate-key error while materializing its first tool call.
        logical_record_id = str(uuid5(NAMESPACE_URL, f"ctf-agent:{run.id}:{logical_id}"))
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
                id=logical_record_id,
                run_id=run.id,
                attempt_id=str(payload.get("attempt_id")) if payload.get("attempt_id") else None,
                engine_type=run.engine_type,
                tool_name=str(tool_name),
                arguments_digest=hashlib.sha256(json.dumps(self._tool_arguments(payload), sort_keys=True, default=str).encode()).hexdigest(),
                status=tool_call.status,
                started_at=tool_call.started_at,
            )
            session.add(logical)
        # Keep finding legacy rows whose id was the unscoped Codex id, while
        # all newly materialized rows use the run-scoped stable UUID above.
        logical = await session.scalar(
            select(LogicalToolCall).where(
                LogicalToolCall.run_id == run.id,
                LogicalToolCall.id.in_((logical_record_id, logical_id)),
            )
        )
        if logical is None:
            logical = LogicalToolCall(
                id=logical_record_id,
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

    def _refresh_run_metrics(
        self,
        run: SolveRun,
        cursor: _MaterializationCursor,
        events: list[RunEvent],
    ) -> None:
        for event in events:
            payload = event.payload_json or {}
            if event.event_type == "agent.turn_completed":
                cursor.agent_steps.add(f"turn:{event.sequence}")
            elif event.event_type == "agent.message":
                # Codex SDK streams turn progress as agent.message items and
                # does not emit the legacy agent.turn_completed event.  Use
                # the stable item id so the UI does not remain at 0 steps.
                item_id = payload.get("item_id")
                if item_id:
                    cursor.agent_steps.add(f"item:{item_id}")
            if event.event_type.startswith("tool."):
                tool_ref = logical_tool_budget_ref(payload)
                if tool_ref:
                    cursor.tool_calls.add(tool_ref)
        run.agent_step_count = len(cursor.agent_steps)
        run.tool_call_count = len(cursor.tool_calls)
        run.run_total_agent_steps = len(cursor.agent_steps)
        run.run_total_logical_tool_calls = len(cursor.tool_calls)
        run.attempt_agent_steps = len(cursor.agent_steps)
        run.attempt_logical_tool_calls = len(cursor.tool_calls)
        run.checkpoint_segment_steps = len(cursor.agent_steps)

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
