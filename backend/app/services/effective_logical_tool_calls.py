"""Single owner for effective logical tool-call identity and traces.

Provider, MCP, Gateway, Runner and Materializer events are execution traces
of one model action.  This service is intentionally small so every layer uses
the same identity algorithm and cannot silently increment the logical budget.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from app.models.run import LogicalToolCall, RunExecutionLease, SolveRun, ToolExecutionTrace


class EffectiveLogicalToolCallService:
    @staticmethod
    def canonical_id(run_id: str, logical_tool_call_id: str) -> str:
        return str(uuid5(NAMESPACE_URL, f"ctf-agent:logical:{run_id}:{logical_tool_call_id}"))

    @staticmethod
    def arguments_digest(arguments: dict) -> str:
        return hashlib.sha256(
            json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True, default=str).encode()
        ).hexdigest()

    async def ensure(
        self,
        session,
        run: SolveRun,
        *,
        logical_tool_call_id: str,
        tool_name: str,
        arguments: dict | None = None,
        status: str = "REQUESTED",
        started_at: datetime | None = None,
        attempt_id: str | None = None,
        deduplicate_by_arguments: bool = False,
    ) -> LogicalToolCall:
        """Get-or-create the one effective row for a provider action."""
        external_id = str(logical_tool_call_id)
        record_id = self.canonical_id(run.id, external_id)
        logical = await session.get(LogicalToolCall, record_id)
        if logical is None:
            if deduplicate_by_arguments and arguments is not None:
                logical = await session.scalar(
                    select(LogicalToolCall)
                    .where(
                        LogicalToolCall.run_id == run.id,
                        LogicalToolCall.tool_name == tool_name,
                        LogicalToolCall.arguments_digest == self.arguments_digest(arguments),
                    )
                    .order_by(LogicalToolCall.created_at.desc())
                )
        if logical is None:
            # A legacy row may use the external id as its primary key. Reuse it
            # instead of creating a second logical call during migration.
            logical = await session.scalar(
                select(LogicalToolCall).where(
                    LogicalToolCall.run_id == run.id,
                    LogicalToolCall.id.in_((record_id, external_id)),
                )
            )
        if logical is None:
            if attempt_id is None:
                lease = await session.scalar(
                    select(RunExecutionLease).where(RunExecutionLease.run_id == run.id)
                )
                attempt_id = lease.attempt_id if lease else None
            logical = LogicalToolCall(
                id=record_id,
                run_id=run.id,
                attempt_id=attempt_id,
                engine_type=run.engine_type,
                tool_name=tool_name,
                arguments_digest=self.arguments_digest(arguments or {}),
                status=status,
                started_at=started_at or datetime.now(UTC),
            )
            session.add(logical)
            await session.flush()
        else:
            logical.tool_name = tool_name
            logical.status = status
            if started_at and logical.started_at is None:
                logical.started_at = started_at
            if arguments is not None:
                logical.arguments_digest = self.arguments_digest(arguments)
        return logical

    async def trace(
        self,
        session,
        logical: LogicalToolCall,
        *,
        execution_layer: str,
        event_type: str,
        external_id: str | None = None,
        payload: object | None = None,
    ) -> ToolExecutionTrace:
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
        ).hexdigest() if payload is not None else ""
        existing = await session.scalar(
            select(ToolExecutionTrace).where(
                ToolExecutionTrace.logical_tool_call_id == logical.id,
                ToolExecutionTrace.execution_layer == execution_layer,
                ToolExecutionTrace.event_type == event_type,
                ToolExecutionTrace.external_id == external_id,
                ToolExecutionTrace.payload_digest == digest,
            )
        )
        if existing:
            return existing
        item = ToolExecutionTrace(
            logical_tool_call_id=logical.id,
            execution_layer=execution_layer,
            event_type=event_type,
            external_id=external_id,
            payload_digest=digest,
        )
        session.add(item)
        await session.flush()
        return item


effective_logical_tool_call_service = EffectiveLogicalToolCallService()
