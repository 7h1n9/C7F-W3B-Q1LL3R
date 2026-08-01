"""Idempotent lifecycle reconciliation for SolveRun.

All entry points that can create a new attempt call this service first.  It
keeps durable execution resources from outliving their Run and makes restart
recovery deterministic instead of turning every inconsistency into another
pause state.
"""

from datetime import UTC, datetime

from sqlalchemy import delete, select

from app.models.multi_agent import AgentTask, ApprovedAction
from app.models.run import RunAttempt, RunExecutionLease, SolveRun, ToolCall, ToolInvocationTicket
from app.models.solver_state import SolverState
from app.schemas.multi_agent import AgentTaskStatus


TERMINAL_RUN_STATUSES = {
    "COMPLETED_SOLVED", "COMPLETED_UNSOLVED", "FAILED_ENGINE", "FAILED_TOOL",
    "FAILED_RUNNER", "TIMEOUT", "POLICY_BLOCKED", "CANCELLED",
}


class RunFinalizer:
    async def build_wp(self, session, run: SolveRun, reason: str) -> dict:
        from app.models.challenge import Challenge

        challenge = await session.get(Challenge, run.challenge_id)
        state = await session.scalar(select(SolverState).where(SolverState.run_id == run.id))
        facts = list((state.confirmed_facts_json or []) if state else [])
        checkpoint = dict(run.recovery_checkpoint_json or {})
        calls = list((await session.scalars(select(ToolCall).where(
            ToolCall.run_id == run.id, ToolCall.status == "COMPLETED"
        ).order_by(ToolCall.created_at))).all())
        tested_fields = sorted({
            str((call.arguments_json or {}).get("test_field"))
            for call in calls
            if call.tool_name == "sql_boolean_compare"
            and isinstance(call.arguments_json, dict)
            and (call.arguments_json or {}).get("test_field")
        })
        return {
            "generated": True,
            "challenge": {"id": run.challenge_id, "name": challenge.name if challenge else ""},
            "target": challenge.target_url if challenge else "",
            "confirmed_facts": facts,
            "completed_stages": sorted({str(item.get("stage") or item.get("source") or "") for item in facts if isinstance(item, dict)}),
            "evidence_summary": {"confirmed_fact_count": len(facts)},
            "tested_fields": tested_fields,
            "completed_tool_calls": [{"id": call.id, "tool": call.tool_name} for call in calls[-50:]],
            "failed_stage": run.current_phase,
            "likely_cause": reason,
            "checkpoint_details": checkpoint,
            "next_manual_steps": ["Inspect the failed stage output and Runner capability.", "Resume after correcting the input or extraction strategy."],
        }

    async def finish_unsolved_with_wp(self, session, run: SolveRun, reason: str) -> dict:
        wp = await self.build_wp(session, run, reason)
        run.status = "COMPLETED_UNSOLVED"
        run.current_phase = "REPORTING"
        run.last_error_code = "COMPLETED_UNSOLVED_WITH_WP"
        run.last_error_message = reason[:4000]
        run.recovery_checkpoint_json = {
            **dict(run.recovery_checkpoint_json or {}),
            "terminal_reason": reason,
            "wp": wp,
            "current_phase": "REPORTING",
        }
        run.finished_at = datetime.now(UTC)
        await self.reconcile(session, run)
        return wp

    async def reconcile(self, session, run: SolveRun) -> dict:
        now = datetime.now(UTC)
        changed = {"leases_deleted": 0, "attempts_closed": 0, "tasks_replanned": 0,
                   "actions_rejected": 0, "tool_calls_closed": 0, "phase_synced": False}

        # A terminal Run owns no executable resource, regardless of which
        # process or request created that resource.
        if str(run.status) in TERMINAL_RUN_STATUSES:
            attempts = list((await session.scalars(select(RunAttempt).where(
                RunAttempt.run_id == run.id, RunAttempt.finished_at.is_(None)
            ))).all())
            for attempt in attempts:
                attempt.status = str(run.status)
                attempt.finished_at = run.finished_at or now
                attempt.error_code = run.last_error_code
                changed["attempts_closed"] += 1
            await session.execute(delete(ToolInvocationTicket).where(ToolInvocationTicket.run_id == run.id))
            await session.execute(delete(RunExecutionLease).where(RunExecutionLease.run_id == run.id))
            changed["leases_deleted"] += 1
            tasks = list((await session.scalars(select(AgentTask).where(
                AgentTask.run_id == run.id, AgentTask.status == AgentTaskStatus.RUNNING.value
            ))).all())
            for task in tasks:
                task.status = AgentTaskStatus.INTERRUPTED.value
                task.cancel_requested = True
                task.lease_expires_at = None
                changed["tasks_replanned"] += 1
            actions = list((await session.scalars(select(ApprovedAction).where(
                ApprovedAction.run_id == run.id, ApprovedAction.status == "ACTIVE"
            ))).all())
            for action in actions:
                action.status = "REJECTED"
                action.compile_error_json = {"code": "RUN_TERMINAL", "message": "Run is terminal."}
                changed["actions_rejected"] += 1
            result = await session.execute(ToolCall.__table__.update().where(
                ToolCall.run_id == run.id, ToolCall.status.in_(["REQUESTED", "STARTED"])
            ).values(status="CANCELLED", finished_at=now))
            changed["tool_calls_closed"] += int(result.rowcount or 0)

        # A live task/action without the execution lease is an interrupted
        # process, not a reason to leave the Run permanently paused.
        leases = list((await session.scalars(select(RunExecutionLease).where(
            RunExecutionLease.run_id == run.id
        ))).all())
        lease = leases[0] if leases else None
        lease_expires_at = None
        if lease is not None:
            lease_expires_at = lease.expires_at.replace(tzinfo=UTC) if lease.expires_at.tzinfo is None else lease.expires_at
        if lease is not None and lease_expires_at <= now:
            await session.execute(delete(ToolInvocationTicket).where(ToolInvocationTicket.lease_id == lease.id))
            await session.delete(lease)
            lease = None
            changed["leases_deleted"] += 1
        if lease is None and str(run.status) not in TERMINAL_RUN_STATUSES:
            tasks = list((await session.scalars(select(AgentTask).where(
                AgentTask.run_id == run.id, AgentTask.status == AgentTaskStatus.RUNNING.value
            ))).all())
            for task in tasks:
                task.status = AgentTaskStatus.NEED_REPLAN.value
                task.lease_expires_at = None
                changed["tasks_replanned"] += 1
            actions = list((await session.scalars(select(ApprovedAction).where(
                ApprovedAction.run_id == run.id, ApprovedAction.status == "ACTIVE"
            ))).all())
            for action in actions:
                action.status = "REJECTED"
                action.compile_error_json = {"code": "SERVICE_RESTART_INTERRUPTED_TASK", "message": "No active Run lease."}
                changed["actions_rejected"] += 1
            attempts = list((await session.scalars(select(RunAttempt).where(
                RunAttempt.run_id == run.id, RunAttempt.status == "RUNNING"
            ))).all())
            for attempt in attempts:
                attempt.status = "ABORTED"
                attempt.error_code = "SERVICE_RESTART_INTERRUPTED_TASK"
                attempt.finished_at = now
                changed["attempts_closed"] += 1

        # Keep the phase projection single-valued.  SolverState is the durable
        # source for the plan JSON, while SolveRun is the API projection.
        state = await session.scalar(select(SolverState).where(SolverState.run_id == run.id))
        if state is not None:
            phase = str(run.current_phase or state.current_phase or "INTAKE")
            if state.current_phase != phase:
                state.current_phase = phase
                changed["phase_synced"] = True
            plan = dict(state.run_plan_json or {})
            if plan.get("current_phase") != phase:
                plan["current_phase"] = phase
                state.run_plan_json = plan
                changed["phase_synced"] = True
        if changed["leases_deleted"] or changed["attempts_closed"] or changed["tasks_replanned"] or changed["actions_rejected"] or changed["tool_calls_closed"] or changed["phase_synced"]:
            await session.commit()
        return changed

    async def reconcile_run(self, session, run_id: str) -> SolveRun:
        run = await session.get(SolveRun, run_id, with_for_update=True)
        if run is None:
            raise ValueError("RUN_NOT_FOUND")
        await self.reconcile(session, run)
        return run


run_finalizer = RunFinalizer()


async def reconcile_run(session, run_id: str) -> SolveRun:
    return await run_finalizer.reconcile_run(session, run_id)
