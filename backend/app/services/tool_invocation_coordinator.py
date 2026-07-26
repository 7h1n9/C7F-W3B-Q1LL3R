"""Run/Attempt/Lease checks shared by all tool invocation paths."""

from datetime import UTC, datetime

from sqlalchemy import select

from app.core.exceptions import DomainError
from app.models.run import RunAttempt, RunExecutionLease, SolveRun
from app.orchestration.state_machine import TERMINAL, RunStatus


class ToolInvocationCoordinator:
    TRANSIENT_STAGES = {"ANALYZING", "PLANNING", "EXECUTING", "EVALUATING", "TESTING"}

    async def validate(self, session, run: SolveRun, *, attempt_id: str | None = None, lease_id: str | None = None) -> dict:
        status = RunStatus(run.status)
        if status in TERMINAL or status in {RunStatus.WAITING_USER, RunStatus.PAUSED_RECOVERY, RunStatus.PAUSED_CHECKPOINT, RunStatus.PAUSED_DEPLOYMENT, RunStatus.WAITING_CONFIGURATION}:
            raise DomainError("RUN_TOOL_NOT_ALLOWED", "Tools are not allowed in a terminal or explicitly paused Run.", {"current_state": run.status}, 409)
        lease = await session.scalar(select(RunExecutionLease).where(RunExecutionLease.run_id == run.id))
        if not lease:
            raise DomainError("RUN_TOOL_NOT_ALLOWED", "An active attempt lease is required for tool execution.", {"run_id": run.id}, 409)
        attempt = await session.get(RunAttempt, lease.attempt_id)
        if attempt_id and (attempt_id != lease.attempt_id or not attempt or attempt.run_id != run.id):
            raise DomainError("STALE_MODEL_TURN", "The model turn belongs to an older Attempt.", {"attempt_id": attempt_id, "active_attempt_id": lease.attempt_id}, 409)
        if lease_id and lease_id != lease.id:
            raise DomainError("STALE_MODEL_TURN", "The model turn belongs to an older execution lease.", {"lease_id": lease_id, "active_lease_id": lease.id}, 409)
        now = datetime.now(UTC)
        expiry = lease.expires_at.replace(tzinfo=UTC) if lease.expires_at and lease.expires_at.tzinfo is None else lease.expires_at
        if not attempt or attempt.status != "RUNNING" or (expiry and expiry <= now):
            raise DomainError("STALE_MODEL_TURN", "The model turn is stale; its Attempt or Lease is no longer active.", {"attempt_id": lease.attempt_id, "lease_id": lease.id}, 409)
        return {"attempt": attempt, "lease": lease, "stage": str(run.current_phase or run.status).upper()}


tool_invocation_coordinator = ToolInvocationCoordinator()
