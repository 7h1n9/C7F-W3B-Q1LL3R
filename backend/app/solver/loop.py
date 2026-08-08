from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

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
from .classification import VulnerabilityClassifier
from .context import RunContext, RuntimeUsage
from .events import SolverAuditEvent, SolverAuditEventType, SolverEvent
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
    audit_event: SolverAuditEvent | None = None

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
        classifier: VulnerabilityClassifier | None = None,
        challenge_context: Any | None = None,
        initial_response: Mapping[str, Any] | None = None,
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
        self.classifier = classifier
        self.challenge_context = challenge_context
        self.initial_response = dict(initial_response or {})

    async def step(self, run_id: str) -> SolverLoopStep:
        state = await self.blackboard.load(run_id)
        if state is None:
            raise KeyError(f"Blackboard not found for run {run_id!r}")

        state = await self._ensure_classified(run_id, state)

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

        planned_execution = ActionExecutionRecord.pending(intent)
        intent = replace(intent, action_id=planned_execution.action_id)
        step_number = self._next_step(state)
        planned_audit = self._audit(
            event_type=SolverAuditEventType.ACTION_PLANNED,
            run_id=run_id,
            step=step_number,
            phase=state.phase,
            action_name=intent.action_name,
            action_id=planned_execution.action_id,
            fingerprint=planned_execution.fingerprint,
            status="PLANNED",
            reason_code="PLANNER_SELECTED",
            evidence_refs=state.evidence_refs,
            blackboard_version=state.version + 1,
        )
        planned_event = SolverEvent(
            event_type="ACTION_PLANNED",
            action=intent.action_name,
            audit_event=planned_audit,
        )
        state = await self.blackboard.update(
            run_id,
            {"control_merge": {"solver_step": step_number, **self._strategy_control(intent)}, "history_append": [planned_event.to_dict()]},
            expected_version=state.version,
        )

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
            return SolverLoopStep(
                "REJECTED",
                updated,
                event,
                intent=intent,
                audit_event=planned_audit,
            )

        usage = self._runtime_usage(state)
        authorize_with_usage = getattr(self.action_authorizer, "authorize_with_usage", None)
        if callable(authorize_with_usage):
            security_decision = authorize_with_usage(intent, self.run_context, usage)
        else:
            security_decision = self.action_authorizer.authorize(intent, self.run_context)
        authorized_audit = self._audit(
            event_type=SolverAuditEventType.ACTION_AUTHORIZED,
            run_id=run_id,
            step=step_number,
            phase=state.phase,
            action_name=intent.action_name,
            action_id=planned_execution.action_id,
            fingerprint=planned_execution.fingerprint,
            status=security_decision.decision.value,
            reason_code=security_decision.reason_code,
            evidence_refs=state.evidence_refs,
            blackboard_version=state.version + 1,
        )
        authorized_event = SolverEvent(
            event_type="ACTION_AUTHORIZED",
            action=intent.action_name,
            payload={"status": security_decision.decision.value},
            audit_event=authorized_audit,
        )
        state = await self.blackboard.update(
            run_id,
            {"history_append": [authorized_event.to_dict()]},
            expected_version=state.version,
        )
        if security_decision.decision is SecurityDecisionType.ALLOW:
            execution = planned_execution.started()
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
                audit_event=self._audit(
                    event_type=SolverAuditEventType.ACTION_STARTED,
                    run_id=run_id,
                    step=step_number,
                    phase=state.phase,
                    action_name=intent.action_name,
                    action_id=execution.action_id,
                    fingerprint=execution.fingerprint,
                    status="STARTED",
                    reason_code="ACTION_CHECKPOINT_WRITTEN",
                    evidence_refs=state.evidence_refs,
                    blackboard_version=state.version + 1,
                ),
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
        feedback = getattr(self.planner, "apply_feedback", None)
        if callable(feedback):
            projected = feedback(
                projected,
                success=result.success,
                new_evidence=bool(observation.evidence_refs or reduction.verified_facts),
            )

        final_audit = None
        if execution is not None:
            final_audit = self._audit(
                event_type=(
                    SolverAuditEventType.ACTION_COMPLETED
                    if execution.state.value == "COMPLETED"
                    else SolverAuditEventType.ACTION_FAILED
                ),
                run_id=run_id,
                step=step_number,
                phase=state.phase,
                action_name=intent.action_name,
                action_id=execution.action_id,
                fingerprint=execution.fingerprint,
                status=execution.state.value,
                reason_code=(
                    "ACTION_COMPLETED"
                    if execution.state.value == "COMPLETED"
                    else execution.error_reason
                ),
                evidence_refs=observation.evidence_refs,
                blackboard_version=state.version + 1,
            )
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
            audit_event=final_audit,
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
                "vulnerability_hypotheses": projected.vulnerability_hypotheses,
                "control_merge": control_merge,
                "history_append": [event.to_dict()],
                "evidence_refs_append": observation.evidence_refs,
            },
            expected_version=state.version,
        )
        return SolverLoopStep(
            step_status,
            updated,
            event,
            intent=intent,
            result=result,
            audit_event=final_audit or authorized_audit,
        )

    async def _ensure_classified(
        self,
        run_id: str,
        state: BlackboardState,
    ) -> BlackboardState:
        control = state.control
        reassess = bool(control.get("reassessment_requested"))
        if state.vulnerability_hypotheses and not reassess:
            return state
        context = self.challenge_context
        if context is None and self.run_context is not None:
            context = self.run_context.challenge
        if context is None:
            return state
        response = self.initial_response or dict(state.knowledge.get("initial_response") or {})
        if self.classifier is not None:
            classified = self.classifier.classify(context, response)
        else:
            classify_task = getattr(self.planner, "_classify_task", None)
            if not callable(classify_task):
                return state
            classified = classify_task(context, response)
        if inspect.isawaitable(classified):
            classified = await classified
        existing = {
            str(item.get("type")): dict(item)
            for item in state.vulnerability_hypotheses
            if isinstance(item, Mapping) and item.get("type")
        }
        merged = []
        for item in classified:
            previous = existing.get(str(item["type"]))
            if previous is not None:
                item = {
                    **item,
                    "failed_attempts": int(previous.get("failed_attempts") or 0),
                    "tested": bool(previous.get("tested", False)),
                }
            merged.append(item)
        attempts = int(control.get("classification_attempts") or 0) + 1
        updated = await self.blackboard.update(
            run_id,
            {
                "vulnerability_hypotheses": merged,
                "control_merge": {
                    "classification_complete": True,
                    "classification_attempts": attempts,
                    "reassessment_requested": False,
                    "classification_source": "heuristic",
                },
                "history_append": [
                    {
                        "type": "VULNERABILITY_CLASSIFIED",
                        "hypothesis_types": [item["type"] for item in merged],
                        "hypothesis_count": len(merged),
                    }
                ],
            },
            expected_version=state.version,
        )
        return updated

    @staticmethod
    def _strategy_control(intent: ActionIntent) -> dict[str, object]:
        vulnerability_type = intent.metadata.get("vulnerability_type")
        if not vulnerability_type:
            return {}
        return {
            "active_vulnerability_type": str(vulnerability_type),
            "strategy_phase": str(intent.metadata.get("strategy_phase") or ""),
            "strategy_chain": list(intent.metadata.get("strategy_chain") or []),
        }

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
            audit_event=self._audit(
                event_type=SolverAuditEventType.ACTION_RECOVERED,
                run_id=run_id,
                step=self._current_step(state),
                phase=state.phase,
                action_name=recovered.action_name,
                action_id=recovered.action_id,
                fingerprint=recovered.fingerprint,
                status="INTERRUPTED",
                reason_code="IN_FLIGHT_ACTION_DETECTED",
                evidence_refs=state.evidence_refs,
                blackboard_version=state.version + 1,
            ),
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
            audit_event=self._audit(
                event_type=SolverAuditEventType.ACTION_INTERRUPTED,
                run_id=run_id,
                step=self._current_step(state),
                phase=state.phase,
                action_name=interrupted.action_name,
                action_id=interrupted.action_id,
                fingerprint=interrupted.fingerprint,
                status="INTERRUPTED",
                reason_code=reason,
                evidence_refs=state.evidence_refs,
                blackboard_version=state.version + 1,
            ),
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
    def _next_step(state: BlackboardState) -> int:
        return int(state.control.get("solver_step") or 0) + 1

    @staticmethod
    def _current_step(state: BlackboardState) -> int:
        return int(state.control.get("solver_step") or 0)

    @staticmethod
    def _audit(
        *,
        event_type: SolverAuditEventType,
        run_id: str,
        step: int,
        phase: str,
        action_name: str | None,
        action_id: str | None,
        fingerprint: str | None,
        status: str | None,
        reason_code: str | None,
        evidence_refs: list[str] | tuple[str, ...],
        blackboard_version: int,
    ) -> SolverAuditEvent:
        return SolverAuditEvent(
            event_type=event_type.value,
            run_id=run_id,
            step=step,
            phase=phase,
            action_name=action_name,
            action_id=action_id,
            fingerprint=fingerprint,
            status=status,
            reason_code=reason_code,
            evidence_refs=tuple(evidence_refs),
            blackboard_version=blackboard_version,
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
