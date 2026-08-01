"""Backend-owned continuous driver for multi_agent_v1 Runs."""

from dataclasses import dataclass

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models.challenge import Challenge
from app.models.multi_agent import VerifiedFact
from app.models.run import FlagCandidate, SolveRun, ToolCall
from app.models.solver_state import SolverState
from app.orchestration.orchestrator import orchestrator
from app.orchestration.state_machine import RunStatus, transition
from app.services.run_finalizer import run_finalizer
from app.services.stage_decider import stage_decider
from app.services.supervisor_progress import supervisor_progress_evaluator
from app.services.writeup_builder import writeup_builder


USER_VISIBLE_TERMINAL = {"WAITING_USER", "COMPLETED_SOLVED", "COMPLETED_UNSOLVED", "CANCELLED"}


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
        return cls(run.id, str(run.status), str(run.current_phase or ""), run.last_error_code, checkpoint.get("wp"))


class RunSupervisor:
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
            if not checkpoint.get("current_wp"):
                checkpoint["current_wp"] = await writeup_builder.build_partial_wp(
                    session, run, str(run.last_error_code or "Waiting for user input.")
                )
                run.recovery_checkpoint_json = checkpoint
                await session.commit()
        return RunOutcome.from_run(run)

    async def continue_until_terminal(self, session, run_id: str, user_message: str | None = None) -> RunOutcome:
        for _ in range(32):
            await session.rollback()
            run = await session.get(SolveRun, run_id)
            if run is None:
                raise ValueError("RUN_NOT_FOUND")
            await run_finalizer.reconcile(session, run)
            await session.refresh(run)
            if str(run.status) in USER_VISIBLE_TERMINAL:
                return await self._outcome(session, run)
            challenge = await session.get(Challenge, run.challenge_id)
            before_keys, before_ledger, candidate, tested = await self._facts(session, run)
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
            await self._recover_internal_pause(session, run, decision.stage)
            run = await session.get(SolveRun, run_id)
            if run is None:
                raise ValueError("RUN_NOT_FOUND")
            if str(run.status) in USER_VISIBLE_TERMINAL:
                return await self._outcome(session, run)
            await orchestrator.start(run_id, user_message if user_message and _ == 0 else None)
            user_message = None
            await session.rollback()
            after = await session.get(SolveRun, run_id)
            if after is None:
                raise ValueError("RUN_NOT_FOUND")
            after_keys, after_ledger, after_candidate, _ = await self._facts(session, after)
            observed = supervisor_progress_evaluator.observe(
                after.recovery_checkpoint_json or {}, stage=decision.stage,
                error_code=after.last_error_code,
                before_facts=before_keys, after_facts=after_keys,
                before_capabilities=set(before_ledger), after_capabilities=set(after_ledger),
                candidate_exists=after_candidate,
            )
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
                await run_finalizer.finish_unsolved_with_wp(session, after, observed.reason)
                return await self._outcome(session, after)
        run = await session.get(SolveRun, run_id)
        if run is None:
            raise ValueError("RUN_NOT_FOUND")
        await run_finalizer.finish_unsolved_with_wp(session, run, "Supervisor cycle limit exhausted.")
        return await self._outcome(session, run)

    async def run_background(self, run_id: str, user_message: str | None = None) -> RunOutcome:
        async with SessionLocal() as session:
            return await self.continue_until_terminal(session, run_id, user_message)


run_supervisor = RunSupervisor()


async def continue_until_terminal(session, run_id: str, user_message: str | None = None) -> RunOutcome:
    return await run_supervisor.continue_until_terminal(session, run_id, user_message)
