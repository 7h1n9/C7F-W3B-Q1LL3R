"""Backend-owned continuous driver for multi_agent_v1 Runs."""

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.models.challenge import Challenge
from app.models.multi_agent import AgentTask, PlannerProposal, VerifiedFact
from app.models.run import FlagCandidate, RunAttempt, RunEvent, RunExecutionLease, RunUserInput, SolveRun, ToolCall
from app.models.solver_state import SolverState
from app.orchestration.state_machine import RunStatus, transition
from app.services.run_finalizer import run_finalizer
from app.services.run_attempts import run_attempt_service
from app.services.stage_decider import stage_decider
from app.services.supervisor_progress import supervisor_progress_evaluator
from app.services.user_input_consumer import consume_user_inputs
from app.services.writeup_builder import writeup_builder
from app.services.events import event_service
from app.services.continuations import continuation_service
from app.core.database import SessionLocal


USER_VISIBLE_TERMINAL = {"WAITING_USER", "COMPLETED_SOLVED", "COMPLETED_UNSOLVED", "CANCELLED"}
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    status: str
    current_phase: str
    error_code: str | None = None
    wp: dict | None = None

    @classmethod
    def from_run(cls, run: SolveRun) -> "RunOutcome":
        checkpoint = run.recovery_checkpoint_json or {}
        return cls(run.id, str(run.status), str(run.current_phase or ""), run.last_error_code, checkpoint.get("wp") or checkpoint.get("current_wp"))


class RunSupervisor:
    def __init__(self) -> None:
        self.run_supervisor_lock: dict[str, asyncio.Lock] = {}
        self._wake_queue: asyncio.Queue[tuple[str, str, str | None]] | None = None
        self._worker_task: asyncio.Task | None = None
        self._queued_runs: set[str] = set()

    async def start_worker(self) -> None:
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._wake_queue = asyncio.Queue()
        self._worker_task = asyncio.create_task(self._worker_loop(), name="run-supervisor-worker")

    async def stop_worker(self) -> None:
        task = self._worker_task
        self._worker_task = None
        self._wake_queue = None
        self._queued_runs.clear()
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def enqueue(self, run_id: str, *, reason: str) -> None:
        await self.start_worker()
        if run_id in self._queued_runs:
            return
        continuation_id: str | None = None
        try:
            async with SessionLocal() as session:
                kind = "USER_INPUT" if reason in {"USER_INPUT_RECEIVED", "USER_INPUT_CONSUMED"} else "CHECKPOINT_RECOVERY"
                current = await session.get(SolveRun, run_id)
                revision = int(current.context_revision or 0) if current is not None else 0
                phase = str(current.current_phase or "") if current is not None else ""
                item = await continuation_service.request(
                    session,
                    run_id,
                    kind=kind,
                    dedupe_key=f"{kind}:{run_id}:{revision}:{phase}",
                    payload={"reason": reason, "context_revision": revision, "phase": phase},
                )
                await session.commit()
                continuation_id = item.id
        except Exception:
            # Compatibility during migration rollout and for isolated tests.
            logger.exception("Continuation persistence unavailable run_id=%s reason=%s", run_id, reason)
        self._queued_runs.add(run_id)
        assert self._wake_queue is not None
        await self._wake_queue.put((run_id, reason, continuation_id))

    async def enqueue_continuation(self, continuation_id: str, run_id: str) -> None:
        await self.start_worker()
        if run_id in self._queued_runs:
            return
        self._queued_runs.add(run_id)
        assert self._wake_queue is not None
        await self._wake_queue.put((run_id, "PERSISTED_CONTINUATION", continuation_id))

    async def recover_pending_continuations(self) -> int:
        async with SessionLocal() as session:
            await continuation_service.recover_stale(session)
            rows = await continuation_service.pending(session)
        for item in rows:
            await self.enqueue_continuation(item.id, item.run_id)
        return len(rows)

    async def _execute_continuation(self, continuation_id: str) -> None:
        async with SessionLocal() as session:
            item = await continuation_service.claim(session, continuation_id)
            if item is None:
                return
            try:
                if item.kind == "RESULT_REVIEW_PENDING":
                    from app.orchestration.multi_agent_orchestrator import multi_agent_orchestrator

                    await multi_agent_orchestrator._resume_result_review(
                        str((item.payload_json or {}).get("producing_task_id") or "")
                    )
                elif item.kind == "USER_INPUT":
                    await self.continue_after_user_input(session, item.run_id)
                else:
                    from app.orchestration.orchestrator import orchestrator

                    await orchestrator.start(item.run_id, (item.payload_json or {}).get("user_message"))
                await continuation_service.complete(session, continuation_id)
            except Exception as error:
                logger.exception("Durable continuation failed id=%s run_id=%s", continuation_id, item.run_id)
                await session.rollback()
                await continuation_service.fail(session, continuation_id, error)

    def _release_run_lock(self, run_id: str, lock: asyncio.Lock) -> None:
        lock.release()
        if not lock.locked() and self.run_supervisor_lock.get(run_id) is lock:
            self.run_supervisor_lock.pop(run_id, None)

    async def _worker_loop(self) -> None:
        assert self._wake_queue is not None
        while True:
            run_id, reason, continuation_id = await self._wake_queue.get()
            self._queued_runs.discard(run_id)
            try:
                if continuation_id is not None:
                    await self._execute_continuation(continuation_id)
                elif reason in {"USER_INPUT_RECEIVED", "USER_INPUT_CONSUMED"}:
                    await self.run_after_user_input_background(run_id)
                else:
                    await self.run_background(run_id)
            except Exception:
                logger.exception("Supervisor wakeup failed run_id=%s reason=%s", run_id, reason)
            finally:
                self._wake_queue.task_done()

    def _asset_mysql(self, challenge: Challenge | None) -> bool:
        metadata = (challenge.metadata_json or {}) if challenge else {}
        return str(metadata.get("adapter") or "").lower() == "asset_warranty" and str(metadata.get("dbms") or "").lower() == "mysql"

    async def _facts(self, session, run: SolveRun) -> tuple[set[str], dict, bool, set[str]]:
        keys = set((await session.scalars(select(VerifiedFact.fact_key).where(
            VerifiedFact.run_id == run.id, VerifiedFact.promotion_status == "VERIFIED"
        ))).all())
        state = await session.scalar(select(SolverState).where(SolverState.run_id == run.id))
        ledger = dict(state.capability_ledger_json or {}) if state else {}
        candidate = bool(await session.scalar(select(FlagCandidate.id).where(
            FlagCandidate.run_id == run.id,
            FlagCandidate.verified.is_(False),
            FlagCandidate.source_artifact_id.is_not(None),
            FlagCandidate.source_tool_call_id.is_not(None),
        )))
        calls = list((await session.scalars(select(ToolCall).where(
            ToolCall.run_id == run.id, ToolCall.tool_name == "sql_boolean_compare", ToolCall.status == "COMPLETED"
        ))).all())
        tested = {str((call.arguments_json or {}).get("test_field")) for call in calls if isinstance(call.arguments_json, dict) and (call.arguments_json or {}).get("test_field")}
        return keys, ledger, candidate, tested

    async def _progress_snapshot(self, session, run_id: str) -> dict[str, int]:
        state = await session.scalar(select(SolverState).where(SolverState.run_id == run_id))
        capability_count = len((state.capability_ledger_json or {}) if state else {})
        return {
            "task_count": int(await session.scalar(select(func.count()).select_from(AgentTask).where(AgentTask.run_id == run_id)) or 0),
            "proposal_count": int(await session.scalar(select(func.count()).select_from(PlannerProposal).where(PlannerProposal.run_id == run_id)) or 0),
            "tool_call_count": int(await session.scalar(select(func.count()).select_from(ToolCall).where(ToolCall.run_id == run_id)) or 0),
            "verified_fact_count": int(await session.scalar(select(func.count()).select_from(VerifiedFact).where(VerifiedFact.run_id == run_id, VerifiedFact.promotion_status == "VERIFIED")) or 0),
            "capability_count": capability_count,
            # Tool-manifest refresh is bootstrap bookkeeping, not a post-input
            # execution decision. Exclude it from the progress signal so a
            # manifest-only attempt cannot satisfy the resume watchdog.
            "event_sequence": int(await session.scalar(select(func.max(RunEvent.sequence)).where(
                RunEvent.run_id == run_id,
                RunEvent.event_type != "attempt.tool_manifest_refreshed",
            )) or 0),
        }

    async def has_unfinished_user_input(self, session, run: SolveRun) -> bool:
        checkpoint = dict(run.recovery_checkpoint_json or {})
        if checkpoint.get("user_input_resume_pending"):
            return True
        last_input = await session.scalar(select(RunUserInput).where(
            RunUserInput.run_id == run.id,
            RunUserInput.status == "CONSUMED",
        ).order_by(RunUserInput.consumed_at.desc(), RunUserInput.created_at.desc()))
        if last_input is None or last_input.consumed_at is None:
            return False
        last_event = await session.scalar(select(RunEvent).where(
            RunEvent.run_id == run.id,
        ).order_by(RunEvent.event_id.desc(), RunEvent.sequence.desc()))
        return bool(last_event and last_event.event_type in {"user_input.consumed", "user.input_consumed"})

    async def _recover_consumed_user_input(self, session, run: SolveRun) -> dict:
        checkpoint = dict(run.recovery_checkpoint_json or {})
        ids = list(checkpoint.get("last_user_input_ids") or [])
        query = select(RunUserInput).where(
            RunUserInput.run_id == run.id,
            RunUserInput.status == "CONSUMED",
        )
        if ids:
            query = query.where(RunUserInput.id.in_(ids))
        rows = list((await session.scalars(query.order_by(RunUserInput.revision, RunUserInput.created_at))).all())
        if not rows:
            return {"items": [], "text": ""}
        return {
            "items": rows,
            "text": "\n\n".join(f"User supplemental input v{item.revision}: {item.content}" for item in rows),
        }

    async def _mark_user_input_no_progress(self, session, run: SolveRun, snapshot: dict) -> RunOutcome:
        run.last_error_code = "USER_INPUT_ACCEPTED_BUT_NO_EXECUTION_PATH"
        run.last_error_message = "User input was consumed, but the multi-agent supervisor did not create a post-input execution decision within 30 seconds."
        run.current_phase = "WAITING_USER"
        run.recovery_checkpoint_json = {
            **(run.recovery_checkpoint_json or {}),
            "current_phase": "WAITING_USER",
            "no_progress_reason": "USER_INPUT_ACCEPTED_BUT_NO_EXECUTION_PATH",
            "user_input_resume_pending": True,
            "progress_snapshot": snapshot,
            "question": "The input was accepted, but no Planner/Task/Tool execution path was created. Choose how to continue.",
            "options": ["retry", "finish_unsolved_wp", "try_alternative_strategy"],
        }
        run.status = RunStatus.WAITING_USER.value
        await event_service.append(session, run.id, "supervisor.no_progress", {
            "run_id": run.id,
            "reason": "USER_INPUT_NO_PROGRESS",
            "progress_snapshot": snapshot,
        })
        return await self._outcome(session, run)

    async def _set_stage(self, session, run: SolveRun, stage: str) -> None:
        run.current_phase = stage
        checkpoint = dict(run.recovery_checkpoint_json or {})
        checkpoint["current_phase"] = stage
        checkpoint["checkpoint_type"] = f"{stage}_ACTIVE"
        run.recovery_checkpoint_json = checkpoint
        state = await session.scalar(select(SolverState).where(SolverState.run_id == run.id))
        if state:
            state.current_phase = stage
            state.run_plan_json = {**(state.run_plan_json or {}), "current_phase": stage}
        await session.commit()

    async def _recover_internal_pause(self, session, run: SolveRun, stage: str) -> None:
        if str(run.status) not in {"PAUSED_RECOVERY", "PAUSED_CHECKPOINT"}:
            return
        if run.last_error_code in {"MYSQL_PREDICATE_NOT_CONFIRMED", "MYSQL_METADATA_STAGE_EMPTY_RESULT"}:
            return
        await self._set_stage(session, run, stage)
        run.last_error_code = None
        run.last_error_message = None
        try:
            transition(run, RunStatus.PLANNING)
        except Exception:
            run.status = RunStatus.PLANNING.value
        await session.commit()

    async def _outcome(self, session, run: SolveRun) -> RunOutcome:
        if str(run.status) == "WAITING_USER":
            checkpoint = dict(run.recovery_checkpoint_json or {})
            resume_broken = checkpoint.get("user_input_resume_pending") and run.last_error_code in {
                "USER_INPUT_ACCEPTED_BUT_NO_EXECUTION_PATH",
                "USER_INPUT_NO_PROGRESS",
            }
            if not resume_broken and not checkpoint.get("current_wp"):
                checkpoint["current_wp"] = await writeup_builder.build_partial_wp(
                    session, run, str(run.last_error_code or "Waiting for user input.")
                )
                run.recovery_checkpoint_json = checkpoint
                run.report_json = checkpoint["current_wp"]
                await session.commit()
        return RunOutcome.from_run(run)

    async def _resolve_waiting_input(self, session, run: SolveRun) -> None:
        checkpoint = dict(run.recovery_checkpoint_json or {})
        checkpoint.pop("question", None)
        checkpoint.pop("options", None)
        checkpoint["waiting_resolved_at"] = datetime.now(UTC).isoformat()
        run.recovery_checkpoint_json = checkpoint
        run.last_error_code = None
        run.last_error_message = None
        try:
            transition(run, RunStatus.PLANNING)
        except Exception:
            run.status = RunStatus.PLANNING.value
        await session.commit()

    async def continue_after_user_input(self, session, run_id: str) -> RunOutcome:
        run = await session.get(SolveRun, run_id)
        if run is None:
            raise ValueError("RUN_NOT_FOUND")
        if str(run.status) in {"COMPLETED_SOLVED", "COMPLETED_UNSOLVED", "CANCELLED"}:
            return RunOutcome(run.id, str(run.status), str(run.current_phase or ""), "RUN_ALREADY_TERMINAL")
        if str(run.status) not in {"WAITING_USER", "PAUSED_CHECKPOINT", "PAUSED_RECOVERY"}:
            return RunOutcome(run.id, str(run.status), str(run.current_phase or ""), "RUN_NOT_WAITING")
        # Establish the resume execution context before consuming input so
        # user_input.consumed carries an attempt_id. The orchestrator reuses
        # this lease instead of bootstrapping a manifest-only attempt.
        await run_attempt_service.reclaim_expired_lease(session, run.id)
        pending_id = await session.scalar(select(RunUserInput.id).where(
            RunUserInput.run_id == run.id,
            RunUserInput.status == "QUEUED",
            RunUserInput.consumed_at.is_(None),
        ).order_by(RunUserInput.revision, RunUserInput.created_at))
        lease = await session.scalar(select(RunExecutionLease).where(RunExecutionLease.run_id == run.id)) if pending_id else None
        attempt = await session.get(RunAttempt, lease.attempt_id) if lease is not None else None
        if pending_id and attempt is None:
            attempt, lease = await run_attempt_service.begin(session, run)
        consumed = await consume_user_inputs(session, run, attempt, wake_supervisor=False)
        if not consumed["items"]:
            consumed = await self._recover_consumed_user_input(session, run)
            if not consumed["items"]:
                return RunOutcome(run.id, str(run.status), str(run.current_phase or ""), "NO_PENDING_USER_INPUT")
            checkpoint = dict(run.recovery_checkpoint_json or {})
            checkpoint["user_input_resume_pending"] = True
            run.recovery_checkpoint_json = checkpoint
            await session.commit()
        if str(run.status) in {"WAITING_USER", "PAUSED_CHECKPOINT", "PAUSED_RECOVERY"}:
            await self._resolve_waiting_input(session, run)
        # Make the resume context explicit for the next Planner snapshot.
        checkpoint = dict(run.recovery_checkpoint_json or {})
        progress = dict((await self._facts(session, run))[1].get("metadata_progress") or {})
        blocked = [f"MYSQL_METADATA_DISCOVERY.{stage}" for stage, item in progress.items() if isinstance(item, dict) and str(item.get("status") or "").upper() == "BLOCKED"]
        checkpoint.update({
            "resume_reason": "USER_INPUT_RECEIVED",
            "blocked_stage": blocked[-1] if blocked else None,
            "suggested_strategy": "try next unblocked metadata stage or alternative bounded extraction",
            "planner_context": {
                "user_inputs": list((run.hints_json or {}).get("user_inputs") or [])[-20:],
                "resume_reason": "USER_INPUT_RECEIVED",
                "blocked_stage": blocked[-1] if blocked else None,
                "metadata_progress": progress,
            },
        })
        run.recovery_checkpoint_json = checkpoint
        await session.commit()
        try:
            outcome = await self.continue_until_terminal(session, run_id, consumed["text"])
            run = await session.get(SolveRun, run_id)
            if run is not None:
                checkpoint = dict(run.recovery_checkpoint_json or {})
                checkpoint["user_input_resume_pending"] = False
                run.recovery_checkpoint_json = checkpoint
                await session.commit()
            return outcome
        except Exception:
            # Keep the durable pending marker so the watchdog can retry after
            # a process crash or a transient infrastructure failure.
            raise

    async def continue_until_terminal(self, session, run_id: str, user_message: str | None = None) -> RunOutcome:
        from app.orchestration.orchestrator import orchestrator

        for _ in range(32):
            await session.rollback()
            run = await session.get(SolveRun, run_id)
            if run is None:
                raise ValueError("RUN_NOT_FOUND")
            await run_finalizer.reconcile(session, run)
            await session.refresh(run)
            if str(run.status) in {"COMPLETED_SOLVED", "COMPLETED_UNSOLVED", "CANCELLED"}:
                return await self._outcome(session, run)
            consumed = await consume_user_inputs(session, run, wake_supervisor=False)
            if consumed["items"] and str(run.status) in {"WAITING_USER", "PAUSED_CHECKPOINT", "PAUSED_RECOVERY"}:
                await self._resolve_waiting_input(session, run)
                run = await session.get(SolveRun, run_id)
            if str(run.status) == "WAITING_USER":
                return await self._outcome(session, run)
            active_lease = await session.scalar(select(RunExecutionLease).where(RunExecutionLease.run_id == run.id))
            # A completed Planner task without its proposal/review/action
            # chain is a controller persistence break, not ordinary lack of
            # tool progress. Never convert it directly into a WP.
            last_planner = await session.scalar(select(AgentTask).where(
                AgentTask.run_id == run.id,
                AgentTask.agent_role == "PLANNER",
                AgentTask.status == "COMPLETED",
            ).order_by(AgentTask.updated_at.desc()))
            if last_planner is not None and active_lease is None:
                proposal_for_planner = await session.scalar(select(PlannerProposal).where(PlannerProposal.created_by_task_id == last_planner.id))
                if proposal_for_planner is None:
                    run.status = RunStatus.WAITING_USER.value
                    run.current_phase = "WAITING_USER"
                    run.last_error_code = "PLANNER_RESULT_NOT_DISPATCHED"
                    run.last_error_message = "Planner completed but no proposal/review/action was dispatched."
                    run.recovery_checkpoint_json = {
                        **(run.recovery_checkpoint_json or {}),
                        "classification": "PLANNER_RESULT_NOT_DISPATCHED",
                        "last_planner_task_id": last_planner.id,
                        "reason": "planner completed but no proposal/review/action was dispatched",
                        "expected_next": "persist planner proposal and dispatch PLAN_REVIEW",
                        "safe_retry": True,
                    }
                    await session.commit()
                    return await self._outcome(session, run)
            if active_lease is not None and run_id not in orchestrator.active_tasks and active_lease.owner_instance_id != run_attempt_service.owner_instance_id:
                return RunOutcome(run.id, str(run.status), str(run.current_phase or ""), "RUN_ALREADY_OWNED")
            counters = dict((run.recovery_checkpoint_json or {}).get("supervisor_counters") or {})
            if active_lease is None and int(counters.get("no_progress_count") or 0) >= 1 and (run.recovery_checkpoint_json or {}).get("resume_reason") != "USER_INPUT_RECEIVED":
                await run_finalizer.finish_unsolved_with_wp(session, run, "NO_PROGRESS_LOOP")
                return await self._outcome(session, run)
            challenge = await session.get(Challenge, run.challenge_id)
            before_keys, before_ledger, candidate, tested = await self._facts(session, run)
            before_snapshot = await self._progress_snapshot(session, run_id)
            metadata = (challenge.metadata_json or {}) if challenge else {}
            declared_fields = {str(item) for item in (metadata.get("fields") or []) if str(item)}
            decision = stage_decider.decide(
                asset_warranty_mysql=self._asset_mysql(challenge),
                verified_fact_keys=before_keys,
                capability_ledger=before_ledger,
                candidate_exists=candidate,
                declared_fields=declared_fields,
                tested_fields=tested,
            )
            if decision.terminal_reason:
                run.last_error_code = decision.terminal_reason
                run.recovery_checkpoint_json = {**(run.recovery_checkpoint_json or {}), "classification": decision.terminal_reason, **decision.details}
                await run_finalizer.finish_unsolved_with_wp(session, run, decision.reason)
                return RunOutcome.from_run(run)
            if decision.requires_user:
                run.status = RunStatus.WAITING_USER.value
                run.current_phase = "WAITING_USER"
                run.last_error_code = "METADATA_ESSENTIAL_STAGES_BLOCKED"
                run.last_error_message = decision.reason
                run.recovery_checkpoint_json = {
                    **(run.recovery_checkpoint_json or {}),
                    "current_phase": "WAITING_USER",
                    "question": decision.reason,
                    "options": ["retry_after_fix", "finish_unsolved_wp", "try_alternative_strategy"],
                    **decision.details,
                }
                await session.commit()
                return await self._outcome(session, run)
            await self._recover_internal_pause(session, run, decision.stage)
            run = await session.get(SolveRun, run_id)
            if run is None:
                raise ValueError("RUN_NOT_FOUND")
            if str(run.status) in USER_VISIBLE_TERMINAL:
                return await self._outcome(session, run)
            await self._set_stage(session, run, decision.stage)
            next_message = consumed["text"] or (user_message if user_message and _ == 0 else None)
            checkpoint = dict(run.recovery_checkpoint_json or {})
            checkpoint["planner_context_consumed"] = True
            run.recovery_checkpoint_json = checkpoint
            await session.commit()
            await orchestrator.start(run_id, next_message)
            user_message = None
            await session.rollback()
            after = await session.get(SolveRun, run_id)
            if after is None:
                raise ValueError("RUN_NOT_FOUND")
            after_keys, after_ledger, after_candidate, _ = await self._facts(session, after)
            after_snapshot = await self._progress_snapshot(session, run_id)
            observed = supervisor_progress_evaluator.observe(
                after.recovery_checkpoint_json or {}, stage=decision.stage,
                error_code=after.last_error_code,
                before_facts=before_keys, after_facts=after_keys,
                before_capabilities=set(before_ledger), after_capabilities=set(after_ledger),
                candidate_exists=after_candidate,
                progress_snapshot_changed=before_snapshot != after_snapshot,
            )
            after.recovery_checkpoint_json = {
                **(after.recovery_checkpoint_json or {}),
                "progress_snapshot": after_snapshot,
            }
            if after_snapshot != before_snapshot:
                after.recovery_checkpoint_json.pop("resume_reason", None)
            await session.commit()
            if str(after.status) in USER_VISIBLE_TERMINAL:
                return await self._outcome(session, after)
            if observed.needs_user:
                after.status = RunStatus.WAITING_USER.value
                after.current_phase = "WAITING_USER"
                after.recovery_checkpoint_json = {**(after.recovery_checkpoint_json or {}), "question": observed.reason, "options": ["retry", "finish_unsolved_wp"], "current_phase": "WAITING_USER"}
                await session.commit()
                return await self._outcome(session, after)
            if observed.terminal_unsolved:
                if (after.recovery_checkpoint_json or {}).get("resume_reason") == "USER_INPUT_RECEIVED" and observed.reason == "NO_PROGRESS_LOOP":
                    return await self._mark_user_input_no_progress(session, after, after_snapshot)
                await run_finalizer.finish_unsolved_with_wp(session, after, observed.reason)
                return await self._outcome(session, after)
        run = await session.get(SolveRun, run_id)
        if run is None:
            raise ValueError("RUN_NOT_FOUND")
        if (run.recovery_checkpoint_json or {}).get("resume_reason") == "USER_INPUT_RECEIVED":
            return await self._mark_user_input_no_progress(session, run, {"reason": "NO_PROGRESS_LOOP"})
        await run_finalizer.finish_unsolved_with_wp(session, run, "NO_PROGRESS_LOOP")
        return await self._outcome(session, run)

    async def run_background(self, run_id: str, user_message: str | None = None) -> RunOutcome:
        lock = self.run_supervisor_lock.setdefault(run_id, asyncio.Lock())
        if lock.locked():
            return RunOutcome(run_id, "RUNNING", "", "RUN_ALREADY_RUNNING")
        await lock.acquire()
        from app.core.database import SessionLocal

        async with SessionLocal() as session:
            try:
                return await self.continue_until_terminal(session, run_id, user_message)
            finally:
                self._release_run_lock(run_id, lock)

    async def run_after_user_input_background(self, run_id: str) -> RunOutcome:
        lock = self.run_supervisor_lock.setdefault(run_id, asyncio.Lock())
        if lock.locked():
            return RunOutcome(run_id, "RUNNING", "", "RUN_ALREADY_RUNNING")
        await lock.acquire()
        from app.core.database import SessionLocal

        async with SessionLocal() as session:
            try:
                outcome = await self.continue_after_user_input(session, run_id)
                if outcome.status in {"COMPLETED_SOLVED", "COMPLETED_UNSOLVED", "CANCELLED"}:
                    return outcome
                baseline = await self._progress_snapshot(session, run_id)
                await asyncio.sleep(30)
                await session.rollback()
                run = await session.get(SolveRun, run_id)
                if run is None or str(run.status) in {"COMPLETED_SOLVED", "COMPLETED_UNSOLVED", "CANCELLED"}:
                    return outcome
                current = await self._progress_snapshot(session, run_id)
                if current == baseline:
                    return await self._mark_user_input_no_progress(session, run, current)
                return RunOutcome.from_run(run)
            finally:
                self._release_run_lock(run_id, lock)


run_supervisor = RunSupervisor()


async def continue_until_terminal(session, run_id: str, user_message: str | None = None) -> RunOutcome:
    return await run_supervisor.continue_until_terminal(session, run_id, user_message)


async def continue_after_user_input(session, run_id: str) -> RunOutcome:
    return await run_supervisor.continue_after_user_input(session, run_id)
