"""Idempotent lifecycle reconciliation for SolveRun.

All entry points that can create a new attempt call this service first.  It
keeps durable execution resources from outliving their Run and makes restart
recovery deterministic instead of turning every inconsistency into another
pause state.
"""

from datetime import UTC, datetime

from sqlalchemy import delete, func, select

from app.models.multi_agent import AgentTask, AgentTaskResult, ApprovedAction, EvidenceLedger, PlannerProposal, SolutionChainNode, VerifiedFact
from app.models.run import RunAttempt, RunExecutionLease, RunUserInput, SolveRun, ToolCall, ToolInvocationTicket
from app.models.solver_state import SolverState
from app.schemas.multi_agent import AgentTaskStatus
from app.services.events import event_service


TERMINAL_RUN_STATUSES = {
    "COMPLETED_SOLVED", "COMPLETED_UNSOLVED", "FAILED_ENGINE", "FAILED_TOOL",
    "FAILED_RUNNER", "TIMEOUT", "POLICY_BLOCKED", "CANCELLED",
}


class RunFinalizer:
    async def build_wp(self, session, run: SolveRun, reason: str) -> dict:
        from app.models.challenge import Challenge

        challenge = await session.get(Challenge, run.challenge_id)
        state = await session.scalar(select(SolverState).where(SolverState.run_id == run.id))
        durable_facts = list((await session.scalars(select(VerifiedFact).where(
            VerifiedFact.run_id == run.id, VerifiedFact.promotion_status == "VERIFIED"
        ).order_by(VerifiedFact.updated_at, VerifiedFact.fact_key))).all())
        facts = [{
            "fact_key": fact.fact_key,
            "fact_type": fact.fact_type,
            "confidence": fact.confidence,
            "value": fact.value_json,
            "evidence_ids": fact.evidence_ids_json or [],
        } for fact in durable_facts]
        if not facts and state:
            facts = list(state.confirmed_facts_json or [])
        checkpoint = dict(run.recovery_checkpoint_json or {})
        calls = list((await session.scalars(select(ToolCall).where(
            ToolCall.run_id == run.id, ToolCall.status == "COMPLETED"
        ).order_by(ToolCall.created_at))).all())
        all_calls = list((await session.scalars(select(ToolCall).where(
            ToolCall.run_id == run.id
        ).order_by(ToolCall.created_at))).all())
        user_inputs = list((await session.scalars(select(RunUserInput).where(
            RunUserInput.run_id == run.id
        ).order_by(RunUserInput.revision, RunUserInput.created_at))).all())
        task_results = list((await session.scalars(select(AgentTaskResult).where(
            AgentTaskResult.task_id.in_(select(AgentTask.id).where(AgentTask.run_id == run.id))
        ))).all())
        chain_nodes = list((await session.scalars(select(SolutionChainNode).where(
            SolutionChainNode.run_id == run.id
        ).order_by(SolutionChainNode.created_at))).all())
        proposals = list((await session.scalars(select(PlannerProposal).where(
            PlannerProposal.run_id == run.id
        ).order_by(PlannerProposal.created_at))).all())
        tested_fields = sorted({
            str((call.arguments_json or {}).get("test_field"))
            for call in calls
            if call.tool_name == "sql_boolean_compare"
            and isinstance(call.arguments_json, dict)
            and (call.arguments_json or {}).get("test_field")
        })
        fact_stage_map = {
            "asset_warranty.valid_baseline": "BUSINESS_BASELINE",
            "asset_warranty.invalid_baseline": "BUSINESS_BASELINE",
            "asset_warranty.mysql_boolean_oracle": "BOOLEAN_ORACLE",
            "asset_warranty.mysql_dbms": "ORACLE_CALIBRATION",
            "asset_warranty.oracle_calibration_matrix": "ORACLE_CALIBRATION",
            "asset_warranty.mysql_version": "MYSQL_METADATA_DISCOVERY",
            "asset_warranty.mysql_version_comment": "MYSQL_METADATA_DISCOVERY",
            "asset_warranty.current_database": "MYSQL_METADATA_DISCOVERY",
            "asset_warranty.mysql_user_tables": "MYSQL_METADATA_DISCOVERY",
            "asset_warranty.mysql_candidate_columns": "MYSQL_METADATA_DISCOVERY",
        }
        ledger = dict(state.capability_ledger_json or {}) if state else {}
        repeated_failures = list((checkpoint.get("tool_failure_counts") or {}).values())
        if not repeated_failures:
            repeated_failures = list((ledger.get("tool_failure_counts") or {}).values())
        failure_history = list(ledger.get("failure_history") or [])
        metadata_failures = [item for item in repeated_failures if item.get("tool_name") == "mysql_metadata_discovery"]
        failed_tools = sorted({
            str(call.tool_name)
            for call in all_calls
            if call.status in {"FAILED", "CANCELLED", "TIMEOUT"}
        } | {
            str(item.get("tool_name"))
            for item in repeated_failures
            if item.get("tool_name")
        })
        next_steps = ["Inspect the failed stage output and Runner capability.", "Resume after correcting the input or extraction strategy."]
        return {
            "generated": True,
            "challenge": {"id": run.challenge_id, "name": challenge.name if challenge else ""},
            "target": challenge.target_url if challenge else "",
            "confirmed_facts": facts,
            "completed_stages": sorted({
                *[str(node.stage) for node in chain_nodes if node.stage],
                *[str(proposal.current_stage) for proposal in proposals if proposal.status in {"PROPOSED", "COMPLETED", "APPROVED"}],
                *[fact_stage_map[fact["fact_key"]] for fact in facts if isinstance(fact, dict) and fact.get("fact_key") in fact_stage_map],
            }),
            "evidence_summary": {
                "confirmed_fact_count": len(durable_facts),
                "evidence_ledger_count": int(await session.scalar(select(func.count()).select_from(EvidenceLedger).where(EvidenceLedger.run_id == run.id)) or 0),
            },
            "user_inputs": [{
                "id": item.id, "revision": item.revision, "content": item.content,
                "status": item.status, "consumed_at": item.consumed_at.isoformat() if item.consumed_at else None,
            } for item in user_inputs],
            "successful_tools": sorted({str(call.tool_name) for call in calls}),
            "failed_tools": failed_tools,
            "repeated_failures": repeated_failures,
            "failure_history": failure_history,
            "metadata_failure_summary": {
                "stage": metadata_failures[-1].get("stage") if metadata_failures else None,
                "error_code": metadata_failures[-1].get("error_code") if metadata_failures else None,
                "repeated_count": metadata_failures[-1].get("count", 0) if metadata_failures else 0,
                "likely_cause": "metadata extractor returned empty result twice" if metadata_failures else None,
                "suggested_fix": "Fix Runner metadata extraction or try an alternative strategy." if metadata_failures else None,
            },
            "task_failure_classifications": [result.failure_classification_json for result in task_results if result.failure_classification_json],
            "tested_fields": tested_fields,
            "completed_tool_calls": [{"id": call.id, "tool": call.tool_name} for call in calls[-50:]],
            "failed_stage": run.current_phase,
            "current_blocker": run.last_error_code or reason,
            "likely_cause": reason,
            "checkpoint_details": checkpoint,
            "next_manual_steps": next_steps,
            "next_steps": next_steps,
        }

    async def finish_unsolved_with_wp(self, session, run: SolveRun, reason: str) -> dict:
        wp = await self.build_wp(session, run, reason)
        run.status = "COMPLETED_UNSOLVED"
        run.current_phase = "REPORTING"
        run.last_error_code = "COMPLETED_UNSOLVED_WITH_WP"
        run.last_error_message = reason[:4000]
        run.report_json = wp
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
                   "actions_rejected": 0, "tool_calls_closed": 0, "phase_synced": False,
                   "wp_rebuilt": False, "terminal_cleaned": False}

        # A terminal Run owns no executable resource, regardless of which
        # process or request created that resource.
        if str(run.status) in TERMINAL_RUN_STATUSES:
            if str(run.status) == "COMPLETED_UNSOLVED":
                existing_wp = (run.recovery_checkpoint_json or {}).get("wp") or {}
                required_wp_fields = {
                    "confirmed_facts",
                    "completed_stages",
                    "failed_tools",
                    "user_inputs",
                    "next_steps",
                }
                if not existing_wp or required_wp_fields - set(existing_wp):
                    rebuilt_wp = await self.build_wp(session, run, str(run.last_error_code or "COMPLETED_UNSOLVED"))
                    run.recovery_checkpoint_json = {
                        **dict(run.recovery_checkpoint_json or {}),
                        "wp": rebuilt_wp,
                    }
                    run.report_json = rebuilt_wp
                    changed["wp_rebuilt"] = True
                elif required_wp_fields - set(run.report_json or {}):
                    run.report_json = existing_wp
                    changed["wp_rebuilt"] = True
            attempts = list((await session.scalars(select(RunAttempt).where(
                RunAttempt.run_id == run.id,
                (RunAttempt.status == "RUNNING") | RunAttempt.finished_at.is_(None),
            ))).all())
            for attempt in attempts:
                attempt.status = str(run.status)
                attempt.finished_at = run.finished_at or now
                attempt.error_code = run.last_error_code
                changed["attempts_closed"] += 1
            await session.execute(delete(ToolInvocationTicket).where(ToolInvocationTicket.run_id == run.id))
            lease_result = await session.execute(delete(RunExecutionLease).where(RunExecutionLease.run_id == run.id))
            changed["leases_deleted"] += int(lease_result.rowcount or 0)
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
            ).values(status="INTERRUPTED", finished_at=now))
            changed["tool_calls_closed"] += int(result.rowcount or 0)
            changed["terminal_cleaned"] = bool(
                changed["attempts_closed"]
                or changed["leases_deleted"]
                or changed["tasks_replanned"]
                or changed["tool_calls_closed"]
            )
            if changed["terminal_cleaned"]:
                await event_service.append(session, run.id, "run.lifecycle.cleaned", {
                    "run_id": run.id,
                    "status": run.status,
                    "attempts_closed": changed["attempts_closed"],
                    "leases_deleted": changed["leases_deleted"],
                    "tasks_interrupted": changed["tasks_replanned"],
                    "tool_calls_closed": changed["tool_calls_closed"],
                })

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
        if any(changed.values()):
            await session.commit()
        return changed

    async def reconcile_run(self, session, run_id: str) -> SolveRun:
        run = await session.get(SolveRun, run_id, with_for_update=True)
        if run is None:
            raise ValueError("RUN_NOT_FOUND")
        await self.reconcile(session, run)
        return run

    async def terminal_reconcile(self, session, run: SolveRun) -> dict:
        """Close every executable resource owned by a terminal Run."""
        if str(run.status) not in TERMINAL_RUN_STATUSES:
            return {}
        return await self.reconcile(session, run)


run_finalizer = RunFinalizer()


async def reconcile_run(session, run_id: str) -> SolveRun:
    return await run_finalizer.reconcile_run(session, run_id)


async def terminal_reconcile(session, run: SolveRun) -> dict:
    return await run_finalizer.terminal_reconcile(session, run)
