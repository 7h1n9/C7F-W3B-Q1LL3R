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
from sqlalchemy.exc import IntegrityError

from app.models.run import LogicalToolCall, RunExecutionLease, SolveRun, ToolExecutionTrace
from app.core.exceptions import DomainError


class EffectiveLogicalToolCallService:
    @staticmethod
    def build_mcp_id(run_id: str, attempt_id: str, turn_id: str, provider_tool_call_id: str) -> str:
        parts = (str(run_id), str(attempt_id), str(turn_id), str(provider_tool_call_id))
        if any(not item or ":" in item for item in parts):
            raise DomainError("LOGICAL_TOOL_ID_INVALID", "MCP logical tool identity components are invalid.", {"parts": parts}, 422)
        return "mcp:" + ":".join(parts)

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
        counts_toward_budget: bool = True,
        logical_kind: str = "TOOL",
        provider_tool_name: str | None = None,
        effective_tool_name: str | None = None,
        turn_id: str | None = None,
        turn_started_at: datetime | None = None,
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
                counts_toward_budget=counts_toward_budget,
                logical_kind=logical_kind,
                provider_tool_name=provider_tool_name or tool_name,
                effective_tool_name=effective_tool_name or tool_name,
                turn_id=turn_id,
                turn_started_at=turn_started_at,
            )
            session.add(logical)
            try:
                # A deterministic UUID plus a database uniqueness constraint
                # is the real idempotency boundary.  The nested transaction
                # lets a losing concurrent writer re-read the winner without
                # rolling back the caller's transaction.
                async with session.begin_nested():
                    await session.flush()
            except IntegrityError:
                logical = await session.get(LogicalToolCall, record_id)
                if logical is None:
                    logical = await session.scalar(
                        select(LogicalToolCall).where(
                            LogicalToolCall.run_id == run.id,
                            LogicalToolCall.id == external_id,
                        )
                    )
                if logical is None:
                    raise
        else:
            incoming_digest = self.arguments_digest(arguments) if arguments is not None else logical.arguments_digest
            if (
                logical.tool_name != tool_name
                or logical.arguments_digest != incoming_digest
                or (provider_tool_name and logical.provider_tool_name not in {None, provider_tool_name})
            ):
                raise DomainError(
                    "LOGICAL_TOOL_ID_COLLISION",
                    "A logical tool identity was reused with different execution data.",
                    {"logical_tool_call_id": external_id, "existing_tool": logical.tool_name, "incoming_tool": tool_name, "existing_arguments_digest": logical.arguments_digest, "incoming_arguments_digest": incoming_digest},
                    409,
                )
            logical.tool_name = tool_name
            logical.status = status
            if started_at and logical.started_at is None:
                logical.started_at = started_at
            if arguments is not None:
                logical.arguments_digest = self.arguments_digest(arguments)
            logical.counts_toward_budget = counts_toward_budget
            logical.logical_kind = logical_kind
            logical.provider_tool_name = provider_tool_name or logical.provider_tool_name or tool_name
            logical.effective_tool_name = effective_tool_name or logical.effective_tool_name or tool_name
            logical.turn_id = turn_id or logical.turn_id
            logical.turn_started_at = turn_started_at or logical.turn_started_at
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
        normalized_external_id = external_id or ""
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
        ).hexdigest() if payload is not None else ""
        existing = await session.scalar(
            select(ToolExecutionTrace).where(
                ToolExecutionTrace.logical_tool_call_id == logical.id,
                ToolExecutionTrace.execution_layer == execution_layer,
                ToolExecutionTrace.event_type == event_type,
                ToolExecutionTrace.external_id == normalized_external_id,
                ToolExecutionTrace.payload_digest == digest,
            )
        )
        if existing:
            return existing
        item = ToolExecutionTrace(
            logical_tool_call_id=logical.id,
            execution_layer=execution_layer,
            event_type=event_type,
            external_id=normalized_external_id,
            payload_digest=digest,
        )
        session.add(item)
        try:
            async with session.begin_nested():
                await session.flush()
        except IntegrityError:
            existing = await session.scalar(
                select(ToolExecutionTrace).where(
                    ToolExecutionTrace.logical_tool_call_id == logical.id,
                    ToolExecutionTrace.execution_layer == execution_layer,
                    ToolExecutionTrace.event_type == event_type,
                    ToolExecutionTrace.external_id == normalized_external_id,
                    ToolExecutionTrace.payload_digest == digest,
                )
            )
            if existing is None:
                raise
            return existing
        return item


effective_logical_tool_call_service = EffectiveLogicalToolCallService()
