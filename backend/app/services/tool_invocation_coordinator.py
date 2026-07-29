"""Run/Attempt/Lease checks shared by all tool invocation paths."""

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.core.exceptions import DomainError
from app.models.multi_agent import AgentTask, ApprovedAction
from app.models.run import LogicalToolCall, RunAttempt, RunExecutionLease, SolveRun, ToolCall
from app.orchestration.state_machine import TERMINAL, RunStatus
from app.services.infrastructure import INFRASTRUCTURE_STATES, reject_infrastructure
from app.tools.registry import load_tool_definitions


class ToolInvocationCoordinator:
    TRANSIENT_STAGES = {"ANALYZING", "PLANNING", "EXECUTING", "EVALUATING", "TESTING"}

    @staticmethod
    def _constraints_match(arguments: dict | None, constraints: dict | None) -> bool:
        args = arguments or {}
        rules = constraints or {}
        required = rules.get("required") or rules.get("required_fields") or {}
        if isinstance(required, list) and any(key not in args for key in required):
            return False
        if isinstance(required, dict) and any(key not in args or (value is not None and args.get(key) != value) for key, value in required.items()):
            return False
        equals = rules.get("equals") or {}
        if isinstance(equals, dict) and any(args.get(key) != value for key, value in equals.items()):
            return False
        allowed = rules.get("allowed_values") or {}
        if isinstance(allowed, dict) and any(args.get(key) not in values for key, values in allowed.items() if isinstance(values, list)):
            return False
        forbidden = rules.get("forbidden_fields") or []
        if isinstance(forbidden, list) and any(key in args for key in forbidden):
            return False
        return True

    async def validate(self, session, run: SolveRun, *, attempt_id: str | None = None, lease_id: str | None = None, agent_task_id: str | None = None, task_lease_token: str | None = None, tool_name: str | None = None, agent_role: str | None = None, approved_action_id: str | None = None, arguments: dict | None = None) -> dict:
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
            idle_expiry = task.idle_deadline_at.replace(tzinfo=UTC) if task.idle_deadline_at and task.idle_deadline_at.tzinfo is None else task.idle_deadline_at
            if idle_expiry and idle_expiry <= now:
                raise DomainError("ROLE_IDLE_TIMEOUT", "The role task has had no model or tool activity within its idle deadline.", {"agent_task_id": agent_task_id}, 408)
            if agent_role and task.agent_role != agent_role:
                raise DomainError("AGENT_SCOPE_INVALID", "The task role does not match the tool-call scope.", {"agent_task_id": agent_task_id, "agent_role": agent_role}, 403)
            if tool_name and tool_name not in (task.allowed_tools_json or []) and not tool_name.startswith("workspace_"):
                raise DomainError("AGENT_TOOL_NOT_ALLOWED", "The task contract does not allow this tool.", {"agent_task_id": agent_task_id, "tool": tool_name}, 422)
            # Production role calls are capability-bearing. A task lease alone
            # is intentionally insufficient to execute a proposed action.
            if not approved_action_id:
                raise DomainError("APPROVED_ACTION_REQUIRED", "A production tool call requires an active ApprovedAction.", {"agent_task_id": agent_task_id}, 403)
            approved = await session.get(ApprovedAction, approved_action_id)
            if approved is not None and approved.compile_status != "COMPILED":
                approved.status = "REJECTED"
                task.status = "NEED_REPLAN"
                run.status = "PAUSED_CHECKPOINT"
                run.current_phase = run.current_phase or "PLANNING"
                await session.flush()
                raise DomainError("APPROVED_ACTION_NOT_COMPILED", "The production task has no compiled ApprovedAction.", {"approved_action_id": approved_action_id}, 409)
            if approved is not None:
                definition = load_tool_definitions().get(approved.tool_name)
                current_schema_hash = definition.schema_hash() if definition else None
                if current_schema_hash != approved.tool_schema_hash:
                    approved.status = "REJECTED"
                    task.status = "NEED_REPLAN"
                    run.status = "PAUSED_CHECKPOINT"
                    run.last_error_code = "TOOL_SCHEMA_VERSION_CHANGED"
                    await session.flush()
                    raise DomainError("TOOL_SCHEMA_VERSION_CHANGED", "The tool schema changed after ApprovedAction compilation.", {"approved_action_id": approved_action_id, "compiled_schema_hash": approved.tool_schema_hash, "current_schema_hash": current_schema_hash}, 409)
            if (
                approved is None
                or approved.run_id != run.id
                or approved.status != "ACTIVE"
                or approved.agent_role != task.agent_role
                or (tool_name and approved.tool_name != tool_name)
                or (approved.expires_at and (approved.expires_at.replace(tzinfo=UTC) if approved.expires_at.tzinfo is None else approved.expires_at) <= now)
            ):
                raise DomainError("APPROVED_ACTION_INVALID", "The ApprovedAction is missing, expired, revoked, or out of scope.", {"approved_action_id": approved_action_id}, 403)
            digest = hashlib.sha256(json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode()).hexdigest()
            if digest != approved.compiled_arguments_digest or arguments != approved.compiled_arguments_json:
                approved.status = "REJECTED"
                task.status = "NEED_REPLAN"
                run.status = "PAUSED_CHECKPOINT"
                run.last_error_code = "APPROVED_ARGUMENT_DIGEST_MISMATCH"
                await session.flush()
                raise DomainError("APPROVED_ARGUMENT_DIGEST_MISMATCH", "Tool arguments are not the compiled ApprovedAction payload.", {"approved_action_id": approved_action_id}, 409)
            tool_rows = int(await session.scalar(select(func.count(ToolCall.id)).where(ToolCall.agent_task_id == task.id, ToolCall.counts_toward_budget.is_(True))) or 0)
            logical_rows = int(await session.scalar(select(func.count(LogicalToolCall.id)).where(LogicalToolCall.run_id == run.id, LogicalToolCall.id.like(f"%agent-task:{task.id}%"), LogicalToolCall.counts_toward_budget.is_(True))) or 0)
            used = max(tool_rows, logical_rows)
            if used >= int((task.budget_json or {}).get("max_logical_calls", 0)):
                raise DomainError("AGENT_TASK_TOOL_BUDGET_EXHAUSTED", "The AgentTask logical-call budget is exhausted.", {"agent_task_id": task.id, "used_calls": used})
            if int(approved.used_logical_calls or 0) >= int(approved.max_logical_calls):
                raise DomainError("APPROVED_ACTION_BUDGET_EXHAUSTED", "The ApprovedAction logical-call budget is exhausted.", {"approved_action_id": approved.id})
            task.last_activity_at = now
            task.heartbeat_at = now
        return {"attempt": attempt, "lease": lease, "task": task, "stage": str(run.current_phase or run.status).upper()}


tool_invocation_coordinator = ToolInvocationCoordinator()
