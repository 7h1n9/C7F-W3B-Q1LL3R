"""Production entry point for the explicit ``solver_v2`` execution mode."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.challenge import Challenge
from app.models.multi_agent import EvidenceLedger
from app.models.run import (
    FlagCandidate,
    FlagProvenance,
    RunAttempt,
    RunExecutionLease,
    SolveRun,
    ToolCall,
)
from app.orchestration.state_machine import TERMINAL, RunStatus, transition
from app.security.action_authorizer import ActionAuthorizer, AllowAllActionAuthorizer
from app.security.default_action_authorizer import DefaultActionAuthorizer
from app.services.events import event_service
from app.services.run_attempts import run_attempt_service

from .action import ActionIntent
from .blackboard import BlackboardState, SolveRunBlackboardStore
from .classification import LLMVulnerabilityClassifier
from .completion import CompletionStatus, SolverCompletionEvaluator
from .context import RunContext
from .context_factory import RunContextFactory
from .evidence import SolverEvidenceAuthority
from .loop import SolverLoop, SolverLoopStep
from .planner import DeterministicPlanner, Planner
from .policy import ActionPolicyValidator
from .reducers import WebObservationReducer
from .state_machine import TaskStateMachine
from .worker import GatewayWorker, RunnerWorker, WorkerManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SolverRunResult:
    run_id: str
    status: str
    current_phase: str
    error_code: str | None = None
    steps: int = 0


class _ProductionPlanner:
    """Add execution metadata without expanding the Planner's action view."""

    def __init__(self, delegate: Planner, context: RunContext, *, backend: str = "gateway") -> None:
        self.delegate = delegate
        self.context = context
        self.backend = backend

    def plan(self, state: BlackboardState, allowed_actions: list[str]) -> ActionIntent | None:
        intent = self.delegate.plan(state, allowed_actions)
        if intent is None:
            return None
        metadata = {
            **dict(intent.metadata),
            "backend": self.backend,
            "run_id": self.context.run_id,
            "allowed_hosts": list(self.context.challenge.target.allowed_hosts),
            "timeout_seconds": self.context.limits.max_runtime_seconds,
        }
        return replace(intent, metadata=metadata)


PlannerFactory = Callable[[RunContext], Planner]
LoopFactory = Callable[..., SolverLoop]
AuthorizerFactory = Callable[[], ActionAuthorizer]


class SolverRuntimeService:
    """Own the production lifecycle around the durable Solver Loop.

    This service is deliberately a new execution mode.  The legacy supervisor
    and orchestrator remain the default and are not called from this path.
    """

    def __init__(
        self,
        *,
        context_factory: RunContextFactory | None = None,
        authorizer_factory: AuthorizerFactory | None = None,
        planner_factory: PlannerFactory | None = None,
        loop_factory: LoopFactory | None = None,
        runner_client: Any | None = None,
    ) -> None:
        self.context_factory = context_factory or RunContextFactory()
        self.authorizer_factory = authorizer_factory or (lambda: DefaultActionAuthorizer())
        self.planner_factory = planner_factory or (lambda _context: DeterministicPlanner())
        self.loop_factory = loop_factory
        self.runner_client = runner_client

    async def run(
        self,
        session: AsyncSession,
        run_id: str,
        user_message: str | None = None,
    ) -> SolverRunResult:
        del user_message  # The v2 Planner receives only Blackboard state.
        run = await session.get(SolveRun, run_id)
        challenge = await session.get(Challenge, run.challenge_id) if run else None
        if run is None or challenge is None:
            return await self._fail(session, run_id, "SOLVER_RUN_NOT_FOUND")
        if RunStatus(run.status) in TERMINAL:
            return self._result(run, 0)

        attempt = None
        lease = None
        attempt_id: str | None = None
        lease_id: str | None = None
        steps = 0
        try:
            # The factory is the only production construction boundary for
            # Challenge/SolveRun -> Solver context.
            context = self.context_factory.build(challenge, run)
            authorizer = self.authorizer_factory()
            if isinstance(authorizer, AllowAllActionAuthorizer):
                raise RuntimeError("production Solver must not use AllowAll")

            repository = SolveRunBlackboardStore(session)
            state = await repository.load(run.id)
            if state is None:
                state = BlackboardState(
                    run_id=run.id,
                    phase="BASELINE",
                    goal={
                        "type": context.challenge.target.challenge_type,
                        "objective": context.challenge.objective,
                    },
                    knowledge={
                        "target_url": context.challenge.target.url,
                        "facts": [],
                        "hypotheses": [],
                        "vulnerabilities": [],
                    },
                    control={"execution_mode": "solver_v2"},
                )
                await repository.save(state)
                run.current_phase = state.phase
                await session.commit()

            execution_backend = "runner" if self.runner_client is not None else "gateway"
            planner = _ProductionPlanner(
                self.planner_factory(context), context, backend=execution_backend
            )
            if self.runner_client is None:
                # Production uses the existing Tool Gateway, which in turn
                # owns Runner dispatch, ToolCall, Artifact, Observation and
                # tool EventService persistence. Tests can still inject the
                # old RunnerWorker boundary with a fake client.
                production_worker = GatewayWorker(session, run, challenge)
            else:
                production_worker = RunnerWorker(self.runner_client)
            worker_manager = WorkerManager(
                workers={"gateway": production_worker, "runner": production_worker},
            )
            if self.loop_factory is None:
                loop = SolverLoop(
                    repository,
                    state_machine=TaskStateMachine(),
                    planner=planner,
                    policy=ActionPolicyValidator(),
                    worker_manager=worker_manager,
                    run_context=context,
                    action_authorizer=authorizer,
                    reducer=WebObservationReducer(),
                    classifier=LLMVulnerabilityClassifier(),
                    challenge_context=context.challenge,
                )
            else:
                loop = self.loop_factory(
                    blackboard=repository,
                    state_machine=TaskStateMachine(),
                    planner=planner,
                    policy=ActionPolicyValidator(),
                    worker_manager=worker_manager,
                    run_context=context,
                    action_authorizer=authorizer,
                    reducer=WebObservationReducer(),
                    classifier=LLMVulnerabilityClassifier(),
                    challenge_context=context.challenge,
                )

            await self._prepare_run(session, run)
            attempt, lease = await run_attempt_service.begin(session, run)
            attempt_id = attempt.id
            lease_id = lease.id
            # Solver v2 enters through RunSupervisor directly and therefore
            # does not pass through the legacy Orchestrator's tool-manifest
            # refresh.  The Gateway requires this immutable per-attempt
            # snapshot before it can authorize networked script execution.
            from app.services.tool_manifest import refresh_runtime_tool_manifest

            manifest = await refresh_runtime_tool_manifest(
                session,
                run,
                attempt,
                challenge,
                mcp_tools=[],
            )
            await session.commit()
            if attempt.tool_manifest_status == "DRIFT" and run.engine_type == "codex_sdk":
                await event_service.append(
                    session,
                    run.id,
                    "run.configuration_blocked",
                    {
                        "code": "TOOL_CATALOG_DRIFT",
                        "missing_expected_tools": manifest.missing_expected_tools,
                        "action": "restart_backend_runner_bridge",
                    },
                )
                return self._result(run, steps)
            if RunStatus(run.status) == RunStatus.PREPARING:
                transition(run, RunStatus.RUNNING)
                await session.commit()
            await event_service.append(session, run.id, "solver.run.started", {"mode": "solver_v2"})

            initial_completed = self._completed_actions(state)
            initial_total_steps = int(run.run_total_agent_steps or 0)
            initial_total_tools = int(run.run_total_logical_tool_calls or 0)
            started = asyncio.get_running_loop().time()
            history_cursor = len(state.history)

            async def bounded_steps() -> str:
                nonlocal steps, history_cursor
                max_steps = context.limits.max_steps
                if max_steps <= 0:
                    return "MAX_AGENT_STEPS_REACHED"
                for _ in range(max_steps):
                    step: SolverLoopStep = await loop.step(run.id)
                    steps += 1
                    state_after = step.state
                    current_run = await session.get(SolveRun, run_id)
                    if current_run is None:
                        raise RuntimeError("run disappeared during Solver execution")
                    completed = self._completed_actions(state_after)
                    new_history = state_after.history[history_cursor:]
                    history_cursor = len(state_after.history)
                    await self._emit_loop_audits(session, current_run, new_history)
                    current_run.current_phase = state_after.phase
                    current_run.agent_step_count = steps
                    current_run.attempt_agent_steps = steps
                    current_run.run_total_agent_steps = initial_total_steps + steps
                    current_run.tool_call_count = max(0, completed - initial_completed)
                    current_run.attempt_logical_tool_calls = current_run.tool_call_count
                    current_run.run_total_logical_tool_calls = initial_total_tools + current_run.tool_call_count
                    await session.commit()
                    await run_attempt_service.heartbeat(session, attempt, lease)
                    await event_service.append(
                        session,
                        current_run.id,
                        "solver.step.completed",
                        {
                            "mode": "solver_v2",
                            "step": steps,
                            "phase": state_after.phase,
                            "event_type": step.event.event_type,
                            "status": step.status,
                        },
                    )
                    if step.finished:
                        return "SOLVER_TERMINATED"
                    if step.status == "WAITING":
                        return "SOLVER_NO_ACTION"
                return "MAX_AGENT_STEPS_REACHED"

            runtime_seconds = context.limits.max_runtime_seconds
            if runtime_seconds and runtime_seconds > 0:
                try:
                    reason = await asyncio.wait_for(bounded_steps(), timeout=runtime_seconds)
                except asyncio.TimeoutError:
                    # ``wait_for`` may cancel an in-flight Blackboard flush and
                    # expire the ORM instance.  Use the stable request id while
                    # the timeout handler rolls the session back.
                    return await self._timeout(session, run_id, steps)
            else:
                reason = await bounded_steps()

            run = await session.get(SolveRun, run.id)
            if run is None:
                raise RuntimeError("run disappeared after Solver execution")
            state = await repository.load(run.id)
            if state is None:
                raise RuntimeError("solver Blackboard disappeared before completion evaluation")
            completion = await self._evaluate_completion(session, run, state)
            if completion.decision is CompletionStatus.SOLVED:
                result = await self._complete_solved(session, run, completion, steps)
            else:
                result = await self._complete_unsolved(session, run, reason, steps, completion=completion)
            elapsed = asyncio.get_running_loop().time() - started
            logger.info("solver_v2 completed run=%s steps=%s elapsed=%.3f", run.id, steps, elapsed)
            return result
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("solver_v2 failed run=%s", run_id)
            return await self._fail(session, run_id, "SOLVER_ENGINE_ERROR")
        finally:
            if attempt is not None:
                current = await session.get(SolveRun, run_id)
                if current is not None:
                    try:
                        # Error handling may rollback the session and expire
                        # the ORM objects returned by ``begin``.  Re-load by
                        # primary key before the finalizer touches counters.
                        current_attempt = (
                            await session.get(RunAttempt, attempt_id) if attempt_id else None
                        )
                        current_lease = (
                            await session.get(RunExecutionLease, lease_id) if lease_id else None
                        )
                        await run_attempt_service.finish(
                            session, current, current_attempt, current_lease
                        )
                    except Exception:
                        logger.exception("failed to finish solver_v2 attempt run=%s", run_id)

    async def _prepare_run(self, session: AsyncSession, run: SolveRun) -> None:
        status = RunStatus(run.status)
        if status == RunStatus.CREATED:
            run.current_phase = "BASELINE"
            transition(run, RunStatus.PREPARING)
            await session.commit()
            return
        if status in {RunStatus.PAUSED_CHECKPOINT, RunStatus.PAUSED_RECOVERY, RunStatus.PAUSED_DEPLOYMENT}:
            transition(run, RunStatus.PLANNING)
            await session.commit()

    async def _complete_unsolved(
        self,
        session: AsyncSession,
        run: SolveRun,
        reason_code: str,
        steps: int,
        *,
        completion=None,
    ) -> SolverRunResult:
        if RunStatus(run.status) not in TERMINAL:
            if RunStatus(run.status) != RunStatus.REPORTING:
                transition(run, RunStatus.REPORTING)
            transition(run, RunStatus.COMPLETED_UNSOLVED)
        run.current_phase = "REPORTING"
        run.last_error_code = reason_code
        run.last_error_message = "Solver reached a controlled bounded stop without a verified finding."
        run.report_json = {
            **dict(run.report_json or {}),
            "solver_mode": "solver_v2",
            "outcome": "UNSOLVED",
            "reason_code": reason_code,
            "completion_decision": completion.decision.value if completion is not None else "UNSOLVED",
            "completion_reason_code": completion.reason_code if completion is not None else "NO_EVALUATION",
        }
        await session.commit()
        if completion is not None:
            await self._emit_completion_event(session, run, completion)
        await event_service.append(
            session,
            run.id,
            "solver.run.completed",
            {"mode": "solver_v2", "status": run.status, "reason_code": reason_code},
        )
        return self._result(run, steps)

    async def _complete_solved(
        self,
        session: AsyncSession,
        run: SolveRun,
        completion,
        steps: int,
    ) -> SolverRunResult:
        candidate_id = await self._materialize_solver_answer(session, run)
        if candidate_id is None:
            # A verified Solver Finding is not automatically a CTF answer.
            # Keep ordinary configuration values (for example a setting key)
            # from crossing the final-answer boundary.
            return await self._complete_unsolved(
                session,
                run,
                "FINAL_ANSWER_REQUIRED",
                steps,
                completion=completion,
            )
        if RunStatus(run.status) not in TERMINAL:
            if RunStatus(run.status) != RunStatus.REPORTING:
                transition(run, RunStatus.REPORTING)
            transition(run, RunStatus.COMPLETED_SOLVED)
        run.current_phase = "REPORTING"
        run.last_error_code = None
        run.last_error_message = None
        run.report_json = {
            **dict(run.report_json or {}),
            "solver_mode": "solver_v2",
            "outcome": "SOLVED",
            "completion_decision": completion.decision.value,
            "completion_reason_code": completion.reason_code,
            "evidence_checked": completion.evidence_checked,
            "missing_requirements": list(completion.missing_requirements),
        }
        await session.commit()
        await self._emit_completion_event(session, run, completion)
        from app.services.verified_flag_stop import verified_flag_stop_controller

        await verified_flag_stop_controller.stop(
            session,
            run,
            candidate_id=candidate_id,
        )
        await event_service.append(
            session,
            run.id,
            "solver.run.completed",
            {"mode": "solver_v2", "status": run.status, "reason_code": completion.reason_code},
        )
        return self._result(run, steps)

    async def _materialize_solver_answer(
        self,
        session: AsyncSession,
        run: SolveRun,
    ) -> str | None:
        checkpoint = run.recovery_checkpoint_json or {}
        state = checkpoint.get("solver_blackboard") if isinstance(checkpoint, dict) else None
        knowledge = state.get("knowledge") if isinstance(state, dict) else {}
        findings = knowledge.get("findings") if isinstance(knowledge, dict) else []
        if not isinstance(findings, list):
            return None
        challenge = await session.get(Challenge, run.challenge_id)
        if challenge is None:
            return None
        for finding in findings:
            if not isinstance(finding, dict) or finding.get("verified") is not True:
                continue
            candidate = str(finding.get("result") or "").strip()
            try:
                valid = bool(candidate) and re.fullmatch(challenge.flag_pattern, candidate) is not None
            except re.error:
                valid = False
            if not valid:
                continue
            refs = [str(item) for item in finding.get("evidence_refs") or [] if str(item)]
            evidence = await session.scalar(
                select(EvidenceLedger).where(
                    EvidenceLedger.run_id == run.id,
                    EvidenceLedger.id.in_(refs),
                    EvidenceLedger.status.in_(["VERIFIED", "ACTIVE"]),
                )
            ) if refs else None
            # Preserve only reproducible request metadata for the final fresh
            # verification.  The expression is rebuilt from the schema names
            # discovered by the Solver; no raw response or challenge secret is
            # copied into the checkpoint.
            request_call = await session.scalar(
                select(ToolCall)
                .where(
                    ToolCall.run_id == run.id,
                    ToolCall.tool_name == "boolean_config_extract",
                    ToolCall.status == "COMPLETED",
                )
                .order_by(ToolCall.created_at)
            )
            request_args = dict(request_call.arguments_json or {}) if request_call else {}
            request_spec = request_args.get("request") if isinstance(request_args.get("request"), dict) else {}
            table = str(finding.get("table") or "").replace('"', '""')
            column = str(finding.get("column") or "").replace('"', '""')
            reproduction_expression = (
                f'SELECT "{column}" FROM "{table}" '
                f'WHERE SUBSTR("{column}",1,5) = \'flag{{\' LIMIT 1'
                if table and column
                else ""
            )
            if request_spec and reproduction_expression:
                checkpoint = dict(run.recovery_checkpoint_json or {})
                checkpoint["solver_reproduction"] = {
                    "request": request_spec,
                    "test_field": request_args.get("test_field"),
                    "baseline_value": request_args.get("baseline_value"),
                    "control_fields": dict(request_args.get("control_fields") or {}),
                    "oracle": dict(request_args.get("oracle") or {}),
                    "target_expression": reproduction_expression,
                    "true_suffix": "' AND ({condition}) -- ",
                    "expression_type": request_args.get("expression_type") or "SOLVER_V2_VERIFICATION",
                    "supporting_evidence_ids": list(request_args.get("supporting_evidence_ids") or refs),
                    "supporting_fact_ids": list(request_args.get("supporting_fact_ids") or []),
                    "source_hypothesis_id": request_args.get("source_hypothesis_id"),
                    "approved_analysis_review_id": request_args.get("approved_analysis_review_id"),
                    "assumption_status": request_args.get("assumption_status") or "VERIFIED",
                }
                run.recovery_checkpoint_json = checkpoint
            existing = await session.scalar(
                select(FlagCandidate).where(
                    FlagCandidate.run_id == run.id,
                    FlagCandidate.candidate == candidate,
                )
            )
            now = datetime.now(UTC)
            if existing is None:
                existing = FlagCandidate(
                    run_id=run.id,
                    candidate=candidate,
                    source_artifact_id=evidence.artifact_id if evidence else None,
                    pattern_matched=True,
                    verified=True,
                    review_state="VALID",
                    first_seen_source_type="SOLVER_V2_EVIDENCE",
                    first_seen_source_id=evidence.artifact_id if evidence else None,
                    first_seen_at=now,
                    source_tool_call_id=evidence.tool_call_id if evidence else None,
                    source_assistance_level="AUTONOMOUS",
                )
                session.add(existing)
                await session.flush()
                session.add(
                    FlagProvenance(
                        run_id=run.id,
                        candidate_id=existing.id,
                        first_seen_source_type="SOLVER_V2_EVIDENCE",
                        first_seen_source_id=evidence.artifact_id if evidence else None,
                        first_seen_at=now,
                        source_artifact_id=evidence.artifact_id if evidence else None,
                        source_tool_call_id=evidence.tool_call_id if evidence else None,
                        verification_source_type="SOLVER_COMPLETION_GATE",
                        verification_source_id=evidence.id if evidence else None,
                        source_is_autonomous=True,
                    )
                )
            else:
                existing.pattern_matched = True
                existing.verified = True
                existing.review_state = "VALID"
            await session.commit()
            return str(existing.id)
        return None

    async def _evaluate_completion(self, session: AsyncSession, run: SolveRun, state: BlackboardState):
        authority = await SolverEvidenceAuthority.from_session(session, run.id)
        decision = SolverCompletionEvaluator().evaluate(
            state,
            evidence_authority=authority,
        )
        return decision

    async def _emit_loop_audits(self, session: AsyncSession, run: SolveRun, history: list[dict]) -> None:
        """Project typed Blackboard audits into the central RunEvent stream."""
        for item in history:
            audit = item.get("audit") if isinstance(item, dict) else None
            if not isinstance(audit, dict):
                continue
            event_type = str(audit.get("event_type") or "")
            if not event_type.startswith("solver."):
                continue
            safe = {
                key: audit[key]
                for key in (
                    "run_id",
                    "step",
                    "phase",
                    "action_name",
                    "action_id",
                    "fingerprint",
                    "status",
                    "reason_code",
                    "evidence_refs",
                    "blackboard_version",
                    "source",
                    "timestamp",
                )
                if key in audit
            }
            await event_service.append(session, run.id, event_type, safe)
            if event_type == "solver.action.started":
                await event_service.append(
                    session,
                    run.id,
                    "solver.tool.called",
                    {
                        "run_id": run.id,
                        "step": audit.get("step", 0),
                        "phase": audit.get("phase", ""),
                        "action_name": audit.get("action_name"),
                        "action_id": audit.get("action_id"),
                        "fingerprint": audit.get("fingerprint"),
                        "status": "CALLED",
                        "reason_code": "WORKER_DISPATCH",
                        "evidence_refs": list(audit.get("evidence_refs") or []),
                        "blackboard_version": audit.get("blackboard_version", 0),
                        "source": "solver",
                    },
                )

        if history:
            last = history[-1]
            if isinstance(last, dict) and last.get("type") in {
                "ACTION_COMPLETED",
                "ACTION_FAILED",
                "ACTION_INTERRUPTED",
                "ACTION_APPROVAL_REQUIRED",
            }:
                audit = last.get("audit") if isinstance(last.get("audit"), dict) else {}
                await event_service.append(
                    session,
                    run.id,
                    "solver.observation.received",
                    {
                        "run_id": run.id,
                        "step": audit.get("step", 0),
                        "phase": audit.get("phase", ""),
                        "action_name": audit.get("action_name") or last.get("action"),
                        "action_id": audit.get("action_id"),
                        "status": last.get("type"),
                        "reason_code": audit.get("reason_code"),
                        "evidence_refs": list(audit.get("evidence_refs") or []),
                        "blackboard_version": audit.get("blackboard_version", 0),
                        "source": "solver",
                    },
                )

    async def _emit_completion_event(self, session: AsyncSession, run: SolveRun, completion) -> None:
        await event_service.append(
            session,
            run.id,
            "solver.completion.evaluated",
            {
                "mode": "solver_v2",
                "decision": completion.decision.value,
                "allowed": completion.allowed,
                "reason_code": completion.reason_code,
                "missing_requirements": list(completion.missing_requirements),
                "evidence_checked": completion.evidence_checked,
            },
        )

    async def _timeout(self, session: AsyncSession, run_id: str, steps: int) -> SolverRunResult:
        await session.rollback()
        run = await session.get(SolveRun, run_id)
        if run is None:
            return await self._fail(session, run_id, "SOLVER_RUN_NOT_FOUND")
        if RunStatus(run.status) not in TERMINAL:
            transition(run, RunStatus.TIMEOUT)
        run.last_error_code = "SOLVER_RUNTIME_TIMEOUT"
        run.last_error_message = "Solver runtime limit reached."
        await session.commit()
        await event_service.append(
            session,
            run.id,
            "solver.run.failed",
            {"mode": "solver_v2", "status": run.status, "error_code": run.last_error_code},
        )
        return self._result(run, steps)

    async def _fail(self, session: AsyncSession, run_id: str, error_code: str) -> SolverRunResult:
        await session.rollback()
        run = await session.get(SolveRun, run_id)
        if run is None:
            return SolverRunResult(run_id, RunStatus.FAILED_ENGINE.value, "", error_code)
        if RunStatus(run.status) not in TERMINAL:
            try:
                transition(run, RunStatus.FAILED_ENGINE)
            except Exception:
                # This is only a last-resort lifecycle guard.  It prevents a
                # production exception from leaving an active run forever.
                run.status = RunStatus.FAILED_ENGINE.value
        run.last_error_code = error_code
        run.last_error_message = "Solver runtime failed before completing the run."
        await session.commit()
        await event_service.append(
            session,
            run.id,
            "solver.run.failed",
            {"mode": "solver_v2", "status": run.status, "error_code": error_code},
        )
        return self._result(run, 0)

    @staticmethod
    def _completed_actions(state: BlackboardState) -> int:
        return sum(str(item.get("type") or "") == "ACTION_COMPLETED" for item in state.history)

    @staticmethod
    def _result(run: SolveRun, steps: int) -> SolverRunResult:
        return SolverRunResult(
            run_id=run.id,
            status=str(run.status),
            current_phase=str(run.current_phase or ""),
            error_code=run.last_error_code,
            steps=steps,
        )


solver_runtime_service = SolverRuntimeService()
