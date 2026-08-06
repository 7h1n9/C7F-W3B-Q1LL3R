"""Guards for recovering abandoned controller execution."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.multi_agent import AgentTask
from app.models.run import RunExecutionLease, ToolCall


PRODUCTION_ROLES = frozenset({"RECON", "EXPLOIT", "VERIFY"})
ACTIVE_TOOL_STATUSES = ("REQUESTED", "STARTED")


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True)
class RecoveryDecision:
    recoverable: bool
    reason: str


class ExecutionRecoveryGuard:
    """Decide whether persisted execution is abandoned.

    A Runner job identifier is durable evidence that dispatch crossed the
    Runner boundary.  Such a call is protected while its execution lease and
    heartbeat are healthy; an expired lease/heartbeat is the explicit signal
    that recovery may proceed.  Runner job existence is intentionally not
    inferred from wall-clock age alone.
    """

    def __init__(self, *, heartbeat_grace_seconds: int = 60) -> None:
        self.heartbeat_grace_seconds = heartbeat_grace_seconds

    def _heartbeat_healthy(self, heartbeat: datetime | None, now: datetime) -> bool:
        value = _aware(heartbeat)
        return value is not None and value >= now - timedelta(seconds=self.heartbeat_grace_seconds)

    def _lease_healthy(self, lease: RunExecutionLease | None, now: datetime) -> bool:
        if lease is None:
            return False
        expires_at = _aware(lease.expires_at)
        return bool(
            expires_at
            and expires_at > now
            and self._heartbeat_healthy(lease.heartbeat_at, now)
        )

    def decision(
        self,
        call: ToolCall,
        *,
        lease: RunExecutionLease | None = None,
        task: AgentTask | None = None,
        now: datetime | None = None,
        timed_out: bool = True,
        runner_job_exists: bool | None = None,
    ) -> RecoveryDecision:
        """Return whether a timed-out call may be recovered.

        ``runner_job_exists`` is optional so the guard remains independent of
        the Runner protocol.  When supplied as ``False`` it is an explicit
        missing-job signal; when omitted, a non-empty job id is treated as
        present and therefore protected unless lease/heartbeat health fails.
        """
        now = _aware(now) or datetime.now(UTC)
        if not timed_out:
            return RecoveryDecision(False, "NOT_TIMED_OUT")
        if call.status == "REQUESTED":
            return RecoveryDecision(True, "REQUESTED_TIMEOUT")
        if call.status != "STARTED":
            return RecoveryDecision(False, "CALL_NOT_ACTIVE")
        if not call.runner_job_id:
            return RecoveryDecision(True, "STARTED_WITHOUT_RUNNER_JOB")
        if runner_job_exists is False:
            return RecoveryDecision(True, "RUNNER_JOB_MISSING")
        if not self._lease_healthy(lease, now):
            return RecoveryDecision(True, "EXECUTION_LEASE_EXPIRED_OR_HEARTBEAT_STALE")
        if task is not None and task.heartbeat_at is not None and not self._heartbeat_healthy(task.heartbeat_at, now):
            return RecoveryDecision(True, "TASK_HEARTBEAT_STALE")
        return RecoveryDecision(False, "RUNNER_JOB_ACTIVE")

    async def active_production_calls(
        self, session: AsyncSession, run_id: str
    ) -> list[ToolCall]:
        """Return unfinished production calls for the Planner barrier."""
        calls = list(
            (
                await session.scalars(
                    select(ToolCall).where(
                        ToolCall.run_id == run_id,
                        ToolCall.status.in_(ACTIVE_TOOL_STATUSES),
                    )
                )
            ).all()
        )
        result: list[ToolCall] = []
        for call in calls:
            if call.agent_role in PRODUCTION_ROLES:
                result.append(call)
                continue
            if call.agent_task_id:
                task = await session.get(AgentTask, call.agent_task_id)
                if task is not None and task.agent_role in PRODUCTION_ROLES:
                    result.append(call)
        return result

    async def protected_production_calls(
        self, session: AsyncSession, run_id: str, *, now: datetime | None = None
    ) -> list[ToolCall]:
        """Return production calls that must not be interrupted by recovery."""
        now = _aware(now) or datetime.now(UTC)
        calls = await self.active_production_calls(session, run_id)
        lease = await session.scalar(
            select(RunExecutionLease).where(RunExecutionLease.run_id == run_id)
        )
        protected: list[ToolCall] = []
        for call in calls:
            task = await session.get(AgentTask, call.agent_task_id) if call.agent_task_id else None
            if call.status == "STARTED" and call.runner_job_id and self._lease_healthy(lease, now):
                if task is None or task.heartbeat_at is None or self._heartbeat_healthy(task.heartbeat_at, now):
                    protected.append(call)
        return protected

    async def recoverable_tool_calls(
        self,
        session: AsyncSession,
        run_id: str,
        *,
        stale_after_seconds: int,
        now: datetime | None = None,
    ) -> list[ToolCall]:
        now = _aware(now) or datetime.now(UTC)
        cutoff = now - timedelta(seconds=stale_after_seconds)
        calls = list(
            (
                await session.scalars(
                    select(ToolCall).where(
                        ToolCall.run_id == run_id,
                        ToolCall.status.in_(ACTIVE_TOOL_STATUSES),
                        ToolCall.created_at < cutoff,
                    )
                )
            ).all()
        )
        lease = await session.scalar(
            select(RunExecutionLease).where(RunExecutionLease.run_id == run_id)
        )
        recoverable: list[ToolCall] = []
        for call in calls:
            task = await session.get(AgentTask, call.agent_task_id) if call.agent_task_id else None
            decision = self.decision(call, lease=lease, task=task, now=now)
            if decision.recoverable:
                recoverable.append(call)
        return recoverable


execution_recovery_guard = ExecutionRecoveryGuard()
