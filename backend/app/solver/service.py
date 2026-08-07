"""Production entry point for the explicit ``solver_v2`` execution mode."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.challenge import Challenge
from app.models.run import RunAttempt, RunExecutionLease, SolveRun
from app.orchestration.state_machine import TERMINAL, RunStatus, transition
from app.security.action_authorizer import ActionAuthorizer, AllowAllActionAuthorizer
from app.security.default_action_authorizer import DefaultActionAuthorizer
from app.services.events import event_service
from app.services.run_attempts import run_attempt_service

from .action import ActionIntent
from .blackboard import BlackboardState, SolveRunBlackboardStore
from .context import RunContext
from .context_factory import RunContextFactory
from .loop import SolverLoop, SolverLoopStep
from .planner import DeterministicPlanner, Planner
from .policy import ActionPolicyValidator
from .reducers import WebObservationReducer
from .state_machine import TaskStateMachine
from .worker import RunnerWorker, WorkerManager

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

    def __init__(self, delegate: Planner, context: RunContext) -> None:
        self.delegate = delegate
        self.context = context

    def plan(self, state: BlackboardState, allowed_actions: list[str]) -> ActionIntent | None:
        intent = self.delegate.plan(state, allowed_actions)
        if intent is None:
            return None
        metadata = {
            **dict(intent.metadata),
            "backend": "runner",
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

            planner = _ProductionPlanner(self.planner_factory(context), context)
            worker_manager = WorkerManager(
                workers={"runner": RunnerWorker(self.runner_client)},
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
                )

            await self._prepare_run(session, run)
            attempt, lease = await run_attempt_service.begin(session, run)
            attempt_id = attempt.id
            lease_id = lease.id
            if RunStatus(run.status) == RunStatus.PREPARING:
                transition(run, RunStatus.RUNNING)
                await session.commit()
            await event_service.append(session, run.id, "solver.run.started", {"mode": "solver_v2"})

            initial_completed = self._completed_actions(state)
            initial_total_steps = int(run.run_total_agent_steps or 0)
            initial_total_tools = int(run.run_total_logical_tool_calls or 0)
            started = asyncio.get_running_loop().time()

            async def bounded_steps() -> str:
                nonlocal steps
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
                    return await self._timeout(session, run.id, steps)
            else:
                reason = await bounded_steps()

            run = await session.get(SolveRun, run.id)
            if run is None:
                raise RuntimeError("run disappeared after Solver execution")
            result = await self._complete_unsolved(session, run, reason, steps)
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
        }
        await session.commit()
        await event_service.append(
            session,
            run.id,
            "solver.run.completed",
            {"mode": "solver_v2", "status": run.status, "reason_code": reason_code},
        )
        return self._result(run, steps)

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
