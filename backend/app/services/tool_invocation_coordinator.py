"""Run/Attempt/Lease checks shared by all tool invocation paths."""

from datetime import UTC, datetime

from sqlalchemy import select

from app.core.exceptions import DomainError
from app.models.multi_agent import AgentTask
from app.models.run import RunAttempt, RunExecutionLease, SolveRun
from app.orchestration.state_machine import TERMINAL, RunStatus
from app.services.infrastructure import INFRASTRUCTURE_STATES, reject_infrastructure


class ToolInvocationCoordinator:
    TRANSIENT_STAGES = {"ANALYZING", "PLANNING", "EXECUTING", "EVALUATING", "TESTING"}

    async def validate(self, session, run: SolveRun, *, attempt_id: str | None = None, lease_id: str | None = None, agent_task_id: str | None = None, task_lease_token: str | None = None, tool_name: str | None = None) -> dict:
        if str(getattr(run, "infrastructure_state", "HEALTHY")) in INFRASTRUCTURE_STATES:
            reject_infrastructure(run)
        status = RunStatus(run.status)
        if status in TERMINAL or status in {RunStatus.WAITING_USER, RunStatus.PAUSED_RECOVERY, RunStatus.PAUSED_CHECKPOINT, RunStatus.PAUSED_DEPLOYMENT, RunStatus.PAUSED_BUDGET, RunStatus.WAITING_CONFIGURATION}:
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
        task = None
        if agent_task_id:
            task = await session.get(AgentTask, agent_task_id)
            if not task or task.run_id != run.id or task.status != "RUNNING":
                raise DomainError("AGENT_TASK_NOT_RUNNING", "The model tool call is not owned by a running AgentTask.", {"agent_task_id": agent_task_id}, 409)
            if not task_lease_token or task.lease_token != task_lease_token:
                raise DomainError("AGENT_TASK_LEASE_INVALID", "The model tool call has an invalid AgentTask lease.", {"agent_task_id": agent_task_id}, 409)
            task_expiry = task.lease_expires_at.replace(tzinfo=UTC) if task.lease_expires_at and task.lease_expires_at.tzinfo is None else task.lease_expires_at
            if task_expiry and task_expiry <= now:
                raise DomainError("AGENT_TASK_LEASE_INVALID", "The AgentTask lease has expired.", {"agent_task_id": agent_task_id}, 409)
            if tool_name and tool_name not in (task.allowed_tools_json or []) and not tool_name.startswith("workspace_"):
                raise DomainError("AGENT_TOOL_NOT_ALLOWED", "The task contract does not allow this tool.", {"agent_task_id": agent_task_id, "tool": tool_name}, 422)
        return {"attempt": attempt, "lease": lease, "task": task, "stage": str(run.current_phase or run.status).upper()}


tool_invocation_coordinator = ToolInvocationCoordinator()
