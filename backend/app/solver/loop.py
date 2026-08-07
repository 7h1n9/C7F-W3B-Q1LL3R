from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass

from app.security.action_authorizer import (
    ActionAuthorizer,
    AllowAllActionAuthorizer,
    SecurityDecisionType,
)

from .action import ActionIntent
from .action_lifecycle import (
    ActionExecutionRecord,
    find_interrupted_action,
    validate_retry_relationship,
)
from .blackboard import BlackboardState
from .blackboard.repository import BlackboardRepository
from .context import RunContext, RuntimeUsage
from .events import SolverEvent
from .knowledge import KnowledgeStore
from .observation import SolverObservation
from .planner import Planner
from .policy import ActionPolicyValidator
from .reducers import ObservationReducer, WebObservationReducer
from .state_machine import TaskStateMachine
from .worker import WorkerManager, WorkerResult


@dataclass(frozen=True)
class SolverLoopStep:
    status: str
    state: BlackboardState
    event: SolverEvent
    intent: ActionIntent | None = None
    result: WorkerResult | None = None

    @property
    def finished(self) -> bool:
        return self.state.phase == "REPORTING" and self.event.event_type == "ACTION_COMPLETED"


class SolverLoop:
    """Durable READ -> PLAN -> VALIDATE -> ACT -> OBSERVE -> WRITE loop."""

    def __init__(
        self,
        blackboard: BlackboardRepository,
        *,
        state_machine: TaskStateMachine,
        planner: Planner,
        policy: ActionPolicyValidator,
        worker_manager: WorkerManager,
        run_context: RunContext | None = None,
        action_authorizer: ActionAuthorizer | None = None,
        reducer: ObservationReducer | None = None,
        knowledge_store: KnowledgeStore | None = None,
    ) -> None:
        self.blackboard = blackboard
        self.state_machine = state_machine
        self.planner = planner
        self.policy = policy
        self.worker_manager = worker_manager
        self.run_context = run_context
        self.action_authorizer = action_authorizer or AllowAllActionAuthorizer()
        self.reducer = reducer or WebObservationReducer()
        self.knowledge_store = knowledge_store or KnowledgeStore()

    async def step(self, run_id: str) -> SolverLoopStep:
        state = await self.blackboard.load(run_id)
        if state is None:
            raise KeyError(f"Blackboard not found for run {run_id!r}")

        interrupted = find_interrupted_action(state)
        if interrupted is not None:
            return await self._recover_interrupted(run_id, state, interrupted)

        allowed_actions = self.state_machine.allowed_actions(state)
        state = await self.blackboard.update(
            run_id,
            {"control_merge": {"allowed_actions": allowed_actions}},
            expected_version=state.version,
        )

        plan = getattr(self.planner, "plan", None)
        intent = plan(state, allowed_actions) if callable(plan) else self.planner.choose(state, allowed_actions)
        if intent is None:
            event = SolverEvent(
                event_type="PLANNER_NO_ACTION",
                payload={"phase": state.phase, "allowed_actions": allowed_actions},
            )
            updated = await self.blackboard.update(
                run_id,
                {"history_append": [event.to_dict()]},
                expected_version=state.version,
            )
            return SolverLoopStep("WAITING", updated, event)

        policy_result = self.policy.validate(state.phase, intent)
        if intent.action_name not in allowed_actions or not policy_result.allowed:
            reason = (
                "action is outside StateMachine allowed actions"
                if intent.action_name not in allowed_actions
                else policy_result.reason
            )
            event = SolverEvent(
                event_type="ACTION_REJECTED",
                action=intent.action_name,
                payload={"phase": state.phase, "reason": reason},
            )
            updated = await self.blackboard.update(
                run_id,
                {"history_append": [event.to_dict()]},
                expected_version=state.version,
            )
            return SolverLoopStep("REJECTED", updated, event, intent=intent)

        usage = self._runtime_usage(state)
        authorize_with_usage = getattr(self.action_authorizer, "authorize_with_usage", None)
        if callable(authorize_with_usage):
            security_decision = authorize_with_usage(intent, self.run_context, usage)
        else:
            security_decision = self.action_authorizer.authorize(intent, self.run_context)
        if security_decision.decision is SecurityDecisionType.ALLOW:
            execution = ActionExecutionRecord.pending(intent).started()
            retry_error = self._retry_error(state, execution)
            if retry_error is not None:
                event = SolverEvent(
                    event_type="ACTION_RETRY_REJECTED",
                    action=intent.action_name,
                    payload={
                        "action_id": execution.action_id,
                        "fingerprint": execution.fingerprint,
                        "retry_of": execution.retry_of,
                        "reason_code": retry_error,
                        "recovery_required": True,
                    },
                )
                updated = await self.blackboard.update(
                    run_id,
                    {"history_append": [event.to_dict()]},
                    expected_version=state.version,
                )
                return SolverLoopStep("RECOVERY_REQUIRED", updated, event, intent=intent)

            started_event = SolverEvent(
                event_type="ACTION_STARTED",
                action=intent.action_name,
                payload={"execution": execution.to_dict()},
            )
            state = await self.blackboard.update(
                run_id,
                {
                    "control_merge": {
                        "active_action": execution.to_dict(),
                        "recovery_feedback": None,
                    },
                    "history_append": [started_event.to_dict()],
                },
                expected_version=state.version,
            )
            try:
                result = await self.worker_manager.execute(intent)
            except asyncio.CancelledError:
                await self._record_interrupted(
                    run_id,
                    state,
                    execution,
                    "WORKER_CANCELLED",
                )
                raise
            except Exception as exc:
                await self._record_interrupted(
                    run_id,
                    state,
                    execution,
                    type(exc).__name__,
                )
                raise
            execution = (
                execution.completed()
                if result.success
                else execution.failed(self._worker_failure_reason(result))
            )
            event_type = "ACTION_COMPLETED" if result.success else "ACTION_FAILED"
            step_status = "CONTINUE"
        else:
            execution = None
            approval_required = security_decision.decision is SecurityDecisionType.REQUIRE_APPROVAL
            result_status = "APPROVAL_REQUIRED" if approval_required else "DENIED"
            result = WorkerResult(
                success=False,
                action_name=intent.action_name,
                output={
                    "status": result_status,
                    "reason": security_decision.reason,
                },
                metadata={
                    "backend": "security",
                    "status": result_status,
                    "policy_id": security_decision.policy_id,
                    "decision": security_decision.decision.value,
                    "reason_code": security_decision.reason_code,
                },
            )
            event_type = "ACTION_APPROVAL_REQUIRED" if approval_required else "ACTION_DENIED"
            step_status = "APPROVAL_REQUIRED" if approval_required else "DENIED"
        observation = SolverObservation.from_worker_result(intent, result)
        reduction = self.reducer.reduce(observation)
        projected = self.knowledge_store.apply(state, reduction)
        next_phase = reduction.next_phase or self.state_machine.next_phase(
            state, intent.action_name, result.status
        )
        projected = projected.model_copy(update={"phase": next_phase})

        event = SolverEvent(
            event_type=event_type,
            action=intent.action_name,
            payload={
                "status": result.status,
                "fact_types": [item.get("type") for item in reduction.verified_facts],
                "hypothesis_types": [item.get("type") for item in reduction.hypotheses],
                "next_phase": next_phase,
                "security_decision": security_decision.decision.value,
                "security_reason": security_decision.reason,
                "security_reason_code": security_decision.reason_code,
            },
        )
        if execution is not None:
            event.payload["execution"] = execution.to_dict()
        control_merge = {}
        if execution is not None:
            control_merge = {
                "active_action": None,
                "last_action_execution": execution.to_dict(),
                "recovery_feedback": None,
            }
        updated = await self.blackboard.update(
            run_id,
            {
                "phase": next_phase,
                "knowledge": projected.knowledge,
                "control": projected.control,
                "control_merge": control_merge,
                "history_append": [event.to_dict()],
                "evidence_refs_append": observation.evidence_refs,
            },
            expected_version=state.version,
        )
        return SolverLoopStep(step_status, updated, event, intent=intent, result=result)

    async def _recover_interrupted(
        self,
        run_id: str,
        state: BlackboardState,
        interrupted: ActionExecutionRecord,
    ) -> SolverLoopStep:
        recovered = interrupted.interrupted("IN_FLIGHT_ACTION_DETECTED")
        feedback = self._recovery_feedback(recovered)
        event = SolverEvent(
            event_type="ACTION_INTERRUPTED",
            action=recovered.action_name,
            payload={
                "execution": recovered.to_dict(),
                "recovery_feedback": feedback,
            },
        )
        updated = await self.blackboard.update(
            run_id,
            {
                "control_merge": {
                    "active_action": None,
                    "last_action_execution": recovered.to_dict(),
                    "recovery_feedback": feedback,
                },
                "history_append": [event.to_dict()],
            },
            expected_version=state.version,
        )
        return SolverLoopStep("RECOVERY_REQUIRED", updated, event)

    async def _record_interrupted(
        self,
        run_id: str,
        state: BlackboardState,
        execution: ActionExecutionRecord,
        reason: str,
    ) -> None:
        interrupted = execution.interrupted(reason)
        feedback = self._recovery_feedback(interrupted)
        event = SolverEvent(
            event_type="ACTION_INTERRUPTED",
            action=interrupted.action_name,
            payload={
                "execution": interrupted.to_dict(),
                "recovery_feedback": feedback,
            },
        )
        await self.blackboard.update(
            run_id,
            {
                "control_merge": {
                    "active_action": None,
                    "last_action_execution": interrupted.to_dict(),
                    "recovery_feedback": feedback,
                },
                "history_append": [event.to_dict()],
            },
            expected_version=state.version,
        )

    @staticmethod
    def _recovery_feedback(execution: ActionExecutionRecord) -> dict[str, object]:
        return {
            "status": "RECOVERY_REQUIRED",
            "reason_code": "IN_FLIGHT_ACTION_DETECTED",
            "action_id": execution.action_id,
            "action_name": execution.action_name,
            "fingerprint": execution.fingerprint,
            "requires_explicit_retry": True,
            "allowed_next_steps": ["retry", "skip", "change_strategy"],
        }

    @staticmethod
    def _retry_error(
        state: BlackboardState,
        execution: ActionExecutionRecord,
    ) -> str | None:
        feedback = state.control.get("recovery_feedback")
        if not isinstance(feedback, Mapping):
            return "RETRY_TARGET_NOT_FOUND" if execution.retry_of else None

        previous = ActionExecutionRecord.from_mapping(feedback)
        same_action = execution.fingerprint == previous.fingerprint
        if same_action and not validate_retry_relationship(execution, previous):
            return "RETRY_OF_REQUIRED"
        if execution.retry_of and not validate_retry_relationship(execution, previous):
            return "RETRY_RELATION_INVALID"
        if execution.retry_of == previous.action_id:
            return "RETRY_ACTION_ID_REUSED" if execution.action_id == previous.action_id else None
        return None

    @staticmethod
    def _worker_failure_reason(result: WorkerResult) -> str:
        return str(
            result.metadata.get("error_code")
            or result.metadata.get("error")
            or result.output.get("error_code")
            or result.output.get("error")
            or result.status
        )

    @staticmethod
    def _runtime_usage(state: BlackboardState) -> RuntimeUsage:
        action_events = {
            "ACTION_COMPLETED",
            "ACTION_DENIED",
            "ACTION_APPROVAL_REQUIRED",
            "ACTION_REJECTED",
        }
        history_types = [str(item.get("type") or "") for item in state.history]
        return RuntimeUsage(
            agent_steps=sum(item in action_events for item in history_types),
            tool_calls=history_types.count("ACTION_COMPLETED"),
        )
