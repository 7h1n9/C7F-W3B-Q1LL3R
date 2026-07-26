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
        reason = None
        if run_count >= maximum:
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
            reason=reason,
        )

    async def enforce(self, session, run: SolveRun, *, attempt_id: str | None = None, turn_started_at=None) -> BudgetDecision:
        decision = await self.check(session, run, attempt_id=attempt_id, turn_started_at=turn_started_at)
        if decision.allowed:
            return decision

        # Budget exhaustion is a controlled pause.  It invalidates the current
        # generation and closes only the active Attempt; no replacement
        # Attempt is created implicitly.
        run.status = "PAUSED_BUDGET"
        run.current_phase = "PAUSED_BUDGET"
        run.last_error_code = "PAUSED_BUDGET"
        run.last_error_message = decision.reason
        run.thread_invalidated = True
        now = datetime.now(UTC)
        attempt = await session.get(RunAttempt, attempt_id) if attempt_id else None
        if attempt and attempt.status == "RUNNING":
            attempt.status = "PAUSED_BUDGET"
            attempt.finished_at = now
            attempt.error_code = "PAUSED_BUDGET"
        lease = await session.scalar(select(RunExecutionLease).where(RunExecutionLease.run_id == run.id))
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
                "thread_invalidated": True,
                "attempt_closed": bool(attempt),
            },
            429,
        )


run_budget_guard = RunBudgetGuard()
