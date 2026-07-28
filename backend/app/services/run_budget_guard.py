"""Durable pre-flight budget checks for every executable tool entry point."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.core.exceptions import DomainError
from app.models.run import LogicalToolCall, RunAttempt, RunExecutionLease, SolveRun


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    effective_max_tool_calls: int
    run_count: int
    attempt_count: int
    turn_count: int
    reserved_count: int = 0
    reserved_turn_count: int = 0
    required_action: bool = False
    required_reserved_count: int = 0
    reason: str | None = None


class RunBudgetGuard:
    SYSTEM_HARD_LIMIT = 120
    # A Codex SDK turn can legitimately contain a bounded batch plus a small
    # amount of evidence inspection.  Eight was too small for the CTF role and
    # made the MCP layer report budget exhaustion after a handful of calls.
    MAX_TOOLS_PER_TURN = 16
    MAX_TOOLS_PER_ATTEMPT = 40

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def _limits(self, run: SolveRun) -> tuple[int, int, int]:
        role = run.role_snapshot_json or {}
        role_limits = role.get("limits") if isinstance(role.get("limits"), dict) else role
        configured = int(run.max_tool_calls or self.SYSTEM_HARD_LIMIT)
        role_limit = int(
            role_limits.get("max_tool_calls")
            or role.get("max_tool_calls")
            or self.SYSTEM_HARD_LIMIT
        )
        return (
            min(configured, role_limit, self.SYSTEM_HARD_LIMIT),
            int(
                role_limits.get("max_tools_per_turn")
                or role.get("max_tools_per_turn")
                or self.MAX_TOOLS_PER_TURN
            ),
            int(
                role_limits.get("max_tools_per_attempt")
                or role.get("max_tools_per_attempt")
                or self.MAX_TOOLS_PER_ATTEMPT
            ),
        )

    async def counts(self, session, run: SolveRun, *, attempt_id: str | None = None, turn_id: str | None = None, turn_started_at=None) -> tuple[int, int, int]:
        effective = LogicalToolCall.counts_toward_budget.is_(True)
        run_count = int(
            await session.scalar(
                select(func.count(func.distinct(LogicalToolCall.id))).where(LogicalToolCall.run_id == run.id, effective)
            )
            or 0
        )
        if attempt_id is None:
            lease = await session.scalar(select(RunExecutionLease).where(RunExecutionLease.run_id == run.id))
            attempt_id = lease.attempt_id if lease else None
        attempt_count = 0
        if attempt_id:
            attempt_count = int(
                await session.scalar(
                    select(func.count(func.distinct(LogicalToolCall.id))).where(
                        LogicalToolCall.run_id == run.id,
                        LogicalToolCall.attempt_id == attempt_id,
                        effective,
                    )
                )
                or 0
            )
        turn_count = 0
        if turn_id is not None:
            turn_count = int(
                await session.scalar(
                    select(func.count(func.distinct(LogicalToolCall.id))).where(
                        LogicalToolCall.run_id == run.id,
                        LogicalToolCall.turn_id == turn_id,
                        effective,
                    )
                )
                or 0
            )
        elif turn_started_at is not None:
            turn_count = int(
                await session.scalar(
                    select(func.count(func.distinct(LogicalToolCall.id))).where(
                        LogicalToolCall.run_id == run.id,
                        LogicalToolCall.created_at >= turn_started_at,
                        effective,
                    )
                )
                or 0
            )
        return run_count, attempt_count, turn_count

    async def check(self, session, run: SolveRun, *, attempt_id: str | None = None, turn_id: str | None = None, turn_started_at=None, required_action: bool = False) -> BudgetDecision:
        if required_action:
            reserved = int(run.reserved_required_action_calls or 0)
            used = int(run.required_action_calls_used or 0)
            allowed = used + reserved < 4
            return BudgetDecision(
                allowed=allowed,
                effective_max_tool_calls=4,
                run_count=0,
                attempt_count=0,
                turn_count=0,
                reserved_count=int(run.reserved_tool_calls or 0),
                reserved_turn_count=0,
                required_action=True,
                required_reserved_count=reserved,
                reason=None if allowed else "REQUIRED_ACTION_BUDGET_EXHAUSTED",
            )
        maximum, per_turn, per_attempt = self._limits(run)
        run_count, attempt_count, turn_count = await self.counts(
            session, run, attempt_id=attempt_id, turn_id=turn_id, turn_started_at=turn_started_at
        )
        reserved = int(run.reserved_tool_calls or 0)
        turn_reserved = int((run.reserved_tool_calls_by_turn_json or {}).get(str(turn_id), 0)) if turn_id else 0
        reason = None
        if run_count + reserved >= maximum:
            reason = "RUN_MAX_TOOL_CALLS"
        elif attempt_count + reserved >= per_attempt:
            reason = "ATTEMPT_TOOL_BUDGET_EXHAUSTED"
        elif turn_count + turn_reserved >= per_turn:
            reason = "TURN_TOOL_BUDGET_EXHAUSTED"
        return BudgetDecision(
            allowed=reason is None,
            effective_max_tool_calls=maximum,
            run_count=run_count,
            attempt_count=attempt_count,
            turn_count=turn_count,
            reserved_count=reserved,
            reserved_turn_count=turn_reserved,
            reason=reason,
        )

    async def enforce(self, session, run: SolveRun, *, attempt_id: str | None = None, turn_id: str | None = None, turn_started_at=None, required_action: bool = False, required_action_kind: str | None = None) -> BudgetDecision:
        lock = self._locks.setdefault(run.id, asyncio.Lock())
        async with lock:
            return await self._enforce(session, run, attempt_id=attempt_id, turn_id=turn_id, turn_started_at=turn_started_at, required_action=required_action, required_action_kind=required_action_kind)

    async def _enforce(self, session, run: SolveRun, *, attempt_id: str | None = None, turn_id: str | None = None, turn_started_at=None, required_action: bool = False, required_action_kind: str | None = None) -> BudgetDecision:
        # The lock is essential: checking a COUNT and inserting the logical
        # call in separate transactions lets concurrent model actions all see
        # the same remaining budget.  MySQL/InnoDB honors FOR UPDATE; SQLite
        # still gets the durable reservation field and serializes the write.
        locked_run = await session.scalar(
            select(SolveRun).where(SolveRun.id == run.id).with_for_update()
        )
        if locked_run is None:
            raise DomainError("RUN_NOT_FOUND", "Run no longer exists.", status_code=404)
        turn_id = turn_id or locked_run.active_turn_id
        decision = await self.check(session, locked_run, attempt_id=attempt_id, turn_id=turn_id, turn_started_at=turn_started_at, required_action=required_action)
        if decision.allowed:
            if required_action:
                locked_run.reserved_required_action_calls = int(locked_run.reserved_required_action_calls or 0) + 1
                by_type = dict(locked_run.reserved_required_action_calls_by_type_json or {})
                if required_action_kind:
                    by_type[required_action_kind] = int(by_type.get(required_action_kind, 0)) + 1
                locked_run.reserved_required_action_calls_by_type_json = by_type
                run.reserved_required_action_calls = locked_run.reserved_required_action_calls
                run.reserved_required_action_calls_by_type_json = by_type
                await session.flush()
                await session.commit()
                return decision
            locked_run.reserved_tool_calls = int(locked_run.reserved_tool_calls or 0) + 1
            by_turn = dict(locked_run.reserved_tool_calls_by_turn_json or {})
            if turn_id:
                by_turn[str(turn_id)] = int(by_turn.get(str(turn_id), 0)) + 1
            locked_run.reserved_tool_calls_by_turn_json = by_turn
            run.reserved_tool_calls = locked_run.reserved_tool_calls
            run.reserved_tool_calls_by_turn_json = by_turn
            await session.flush()
            # Reservations must be visible to concurrent MCP calls before the
            # eventual ToolCall transaction is committed.
            await session.commit()
            return decision

        if decision.reason == "REQUIRED_ACTION_BUDGET_EXHAUSTED":
            raise DomainError(
                decision.reason,
                "The bounded fallback action budget is exhausted.",
                {"required_reserved_count": decision.required_reserved_count, "required_action_used": int(run.required_action_calls_used or 0), "required_action_budget": 4},
                429,
                stage="REQUIRED_ACTION_BUDGET",
                retryable=False,
            )
        if decision.reason == "TURN_TOOL_BUDGET_EXHAUSTED":
            raise DomainError(
                decision.reason,
                "The current model turn exceeded its tool budget.",
                {
                    "effective_max_tool_calls": decision.effective_max_tool_calls,
                    "turn_count": decision.turn_count,
                    "reserved_turn_count": decision.reserved_turn_count,
                    "per_turn_limit": self._limits(run)[1],
                },
                429,
                stage="BUDGET_GUARD",
                retryable=False,
            )

        # Budget exhaustion is a controlled pause.  It invalidates the current
        # generation and closes only the active Attempt; no replacement
        # Attempt is created implicitly.
        locked_run.status = "PAUSED_BUDGET"
        # PAUSED_BUDGET is a Run status, not a solver phase.
        locked_run.last_error_code = "PAUSED_BUDGET"
        locked_run.last_error_message = decision.reason
        locked_run.thread_invalidated = True
        run.status = locked_run.status
        run.last_error_code = locked_run.last_error_code
        run.last_error_message = locked_run.last_error_message
        run.thread_invalidated = True
        now = datetime.now(UTC)
        attempt = await session.get(RunAttempt, attempt_id) if attempt_id else None
        if attempt and attempt.status == "RUNNING":
            attempt.status = "PAUSED_BUDGET"
            attempt.finished_at = now
            attempt.error_code = "PAUSED_BUDGET"
        lease = await session.scalar(select(RunExecutionLease).where(RunExecutionLease.run_id == locked_run.id))
        if lease:
            await session.delete(lease)
        await session.commit()
        raise DomainError(
            decision.reason or "PAUSED_BUDGET",
            "Tool budget exhausted before execution; the Attempt was closed without creating a replacement.",
            {
                "effective_max_tool_calls": decision.effective_max_tool_calls,
                "run_count": decision.run_count,
                "attempt_count": decision.attempt_count,
                "turn_count": decision.turn_count,
                "reserved_count": decision.reserved_count,
                "thread_invalidated": True,
                "attempt_closed": bool(attempt),
            },
            429,
        )

    async def release(self, session, run: SolveRun, amount: int = 1, turn_id: str | None = None, required_action: bool = False, required_action_kind: str | None = None) -> None:
        """Transfer an in-flight reservation to a persisted logical call."""
        if required_action:
            amount = max(1, amount)
            run.reserved_required_action_calls = max(0, int(run.reserved_required_action_calls or 0) - amount)
            run.required_action_calls_used = int(run.required_action_calls_used or 0) + amount
            by_type = dict(run.reserved_required_action_calls_by_type_json or {})
            if required_action_kind and by_type.get(required_action_kind, 0):
                by_type[required_action_kind] = max(0, int(by_type[required_action_kind]) - amount)
                if not by_type[required_action_kind]:
                    by_type.pop(required_action_kind, None)
            run.reserved_required_action_calls_by_type_json = by_type
            await session.flush()
            return
        run.reserved_tool_calls = max(0, int(run.reserved_tool_calls or 0) - max(1, amount))
        by_turn = dict(run.reserved_tool_calls_by_turn_json or {})
        if turn_id and by_turn.get(str(turn_id), 0):
            by_turn[str(turn_id)] = max(0, int(by_turn[str(turn_id)]) - max(1, amount))
            if not by_turn[str(turn_id)]:
                by_turn.pop(str(turn_id), None)
        run.reserved_tool_calls_by_turn_json = by_turn
        await session.flush()


run_budget_guard = RunBudgetGuard()
