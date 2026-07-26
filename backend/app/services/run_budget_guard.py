"""Durable pre-flight budget checks for every executable tool entry point."""

from __future__ import annotations

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
    reason: str | None = None


class RunBudgetGuard:
    SYSTEM_HARD_LIMIT = 120
    MAX_TOOLS_PER_TURN = 8
    MAX_TOOLS_PER_ATTEMPT = 40

    def _limits(self, run: SolveRun) -> tuple[int, int, int]:
        role = run.role_snapshot_json or {}
        configured = int(run.max_tool_calls or self.SYSTEM_HARD_LIMIT)
        role_limit = int(role.get("max_tool_calls") or self.SYSTEM_HARD_LIMIT)
        return (
            min(configured, role_limit, self.SYSTEM_HARD_LIMIT),
            int(role.get("max_tools_per_turn") or self.MAX_TOOLS_PER_TURN),
            int(role.get("max_tools_per_attempt") or self.MAX_TOOLS_PER_ATTEMPT),
        )

    async def counts(self, session, run: SolveRun, *, attempt_id: str | None = None, turn_started_at=None) -> tuple[int, int, int]:
        run_count = int(
            await session.scalar(
                select(func.count(func.distinct(LogicalToolCall.id))).where(LogicalToolCall.run_id == run.id)
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
                        LogicalToolCall.run_id == run.id, LogicalToolCall.attempt_id == attempt_id
                    )
                )
                or 0
            )
        turn_count = 0
        if turn_started_at is not None:
            turn_count = int(
                await session.scalar(
                    select(func.count(func.distinct(LogicalToolCall.id))).where(
                        LogicalToolCall.run_id == run.id,
                        LogicalToolCall.created_at >= turn_started_at,
                    )
                )
                or 0
            )
        return run_count, attempt_count, turn_count

    async def check(self, session, run: SolveRun, *, attempt_id: str | None = None, turn_started_at=None) -> BudgetDecision:
        maximum, per_turn, per_attempt = self._limits(run)
        run_count, attempt_count, turn_count = await self.counts(
            session, run, attempt_id=attempt_id, turn_started_at=turn_started_at
        )
        reserved = int(run.reserved_tool_calls or 0)
        reason = None
        if run_count + reserved >= maximum:
            reason = "RUN_MAX_TOOL_CALLS"
        elif attempt_count >= per_attempt:
            reason = "ATTEMPT_TOOL_BUDGET_EXHAUSTED"
        elif turn_count >= per_turn:
            reason = "TURN_TOOL_BUDGET_EXHAUSTED"
        return BudgetDecision(
            allowed=reason is None,
            effective_max_tool_calls=maximum,
            run_count=run_count,
            attempt_count=attempt_count,
            turn_count=turn_count,
            reserved_count=reserved,
            reason=reason,
        )

    async def enforce(self, session, run: SolveRun, *, attempt_id: str | None = None, turn_started_at=None) -> BudgetDecision:
        # The lock is essential: checking a COUNT and inserting the logical
        # call in separate transactions lets concurrent model actions all see
        # the same remaining budget.  MySQL/InnoDB honors FOR UPDATE; SQLite
        # still gets the durable reservation field and serializes the write.
        locked_run = await session.scalar(
            select(SolveRun).where(SolveRun.id == run.id).with_for_update()
        )
        if locked_run is None:
            raise DomainError("RUN_NOT_FOUND", "Run no longer exists.", status_code=404)
        decision = await self.check(session, locked_run, attempt_id=attempt_id, turn_started_at=turn_started_at)
        if decision.allowed:
            locked_run.reserved_tool_calls = int(locked_run.reserved_tool_calls or 0) + 1
            run.reserved_tool_calls = locked_run.reserved_tool_calls
            await session.flush()
            return decision

        # Budget exhaustion is a controlled pause.  It invalidates the current
        # generation and closes only the active Attempt; no replacement
        # Attempt is created implicitly.
        locked_run.status = "PAUSED_BUDGET"
        locked_run.current_phase = "PAUSED_BUDGET"
        locked_run.last_error_code = "PAUSED_BUDGET"
        locked_run.last_error_message = decision.reason
        locked_run.thread_invalidated = True
        run.status = locked_run.status
        run.current_phase = locked_run.current_phase
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

    async def release(self, session, run: SolveRun, amount: int = 1) -> None:
        """Transfer an in-flight reservation to a persisted logical call."""
        run.reserved_tool_calls = max(0, int(run.reserved_tool_calls or 0) - max(1, amount))
        await session.flush()


run_budget_guard = RunBudgetGuard()
