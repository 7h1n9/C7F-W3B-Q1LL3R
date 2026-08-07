from __future__ import annotations

from dataclasses import dataclass

from .action import ActionIntent
from .blackboard import BlackboardState
from .blackboard.repository import BlackboardRepository
from .events import SolverEvent
from .planner import Planner
from .policy import ActionPolicyValidator
from .state_machine import TaskStateMachine
from .worker import Worker, WorkerResult


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
        worker: Worker,
    ) -> None:
        self.blackboard = blackboard
        self.state_machine = state_machine
        self.planner = planner
        self.policy = policy
        self.worker = worker

    async def step(self, run_id: str) -> SolverLoopStep:
        state = await self.blackboard.load(run_id)
        if state is None:
            raise KeyError(f"Blackboard not found for run {run_id!r}")

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

        result = await self.worker.execute(intent)
        next_phase = self.state_machine.next_phase(state, intent.action_name, result.status)
        knowledge = dict(state.knowledge)
        observations = list(knowledge.get("observations") or [])
        observations.append(
            {
                "action": intent.action_name,
                "status": result.status,
                "observation": dict(result.observation),
            }
        )
        knowledge["observations"] = observations
        if result.facts:
            knowledge["facts"] = [*(knowledge.get("facts") or []), *result.facts]
        if result.hypotheses:
            knowledge["hypotheses"] = [*(knowledge.get("hypotheses") or []), *result.hypotheses]

        event = SolverEvent(
            event_type="ACTION_COMPLETED",
            action=intent.action_name,
            payload={"status": result.status, "observation": dict(result.observation)},
        )
        updated = await self.blackboard.update(
            run_id,
            {
                "phase": next_phase,
                "knowledge": knowledge,
                "history_append": [event.to_dict()],
                "evidence_refs_append": result.evidence_refs,
            },
            expected_version=state.version,
        )
        return SolverLoopStep("CONTINUE", updated, event, intent=intent, result=result)
