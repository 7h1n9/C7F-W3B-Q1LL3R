from __future__ import annotations

from dataclasses import dataclass

from .action import ActionIntent
from .blackboard import Blackboard, BlackboardState
from .planner import NoopPlanner, Planner
from .policy import ActionPolicyValidator
from .state_machine import TaskStateMachine
from .worker import NoopWorker, Worker, WorkerResult


@dataclass(frozen=True)
class CoordinatorStep:
    status: str
    state: BlackboardState
    intent: ActionIntent | None = None
    result: WorkerResult | None = None


class Coordinator:
    """One-tick READ -> PLAN -> ACT -> OBSERVE -> WRITE loop."""

    def __init__(
        self,
        blackboard: Blackboard,
        *,
        state_machine: TaskStateMachine | None = None,
        planner: Planner | None = None,
        worker: Worker | None = None,
        policy: ActionPolicyValidator | None = None,
    ) -> None:
        self.blackboard = blackboard
        self.state_machine = state_machine or TaskStateMachine()
        self.planner = planner or NoopPlanner()
        self.worker = worker or NoopWorker()
        self.policy = policy or ActionPolicyValidator()

    async def step(self, run_id: str) -> CoordinatorStep:
        state = self.blackboard.read(run_id)
        allowed_actions = self.state_machine.allowed_actions(state)
        self.blackboard.update(run_id, allowed_actions=allowed_actions)
        state = self.blackboard.read(run_id)

        plan = getattr(self.planner, "plan", None)
        intent = plan(state, allowed_actions) if callable(plan) else self.planner.choose(state, allowed_actions)
        if intent is None:
            updated = self.blackboard.update(
                run_id,
                event={"type": "planner.no_action", "phase": state.phase},
            )
            return CoordinatorStep("WAITING", updated)

        if not self.state_machine.is_allowed(state, intent.action_name):
            updated = self.blackboard.update(
                run_id,
                event={
                    "type": "intent.rejected",
                    "action": intent.action_name,
                    "phase": state.phase,
                    "reason": "action is outside StateMachine allowed actions",
                },
            )
            return CoordinatorStep("REJECTED", updated, intent=intent)

        policy_result = self.policy.validate(state.phase, intent)
        if not policy_result.allowed:
            updated = self.blackboard.update(
                run_id,
                event={
                    "type": "intent.rejected",
                    "action": intent.action_name,
                    "phase": state.phase,
                    "reason": policy_result.reason,
                },
            )
            return CoordinatorStep("REJECTED", updated, intent=intent)

        result = await self.worker.execute(intent)
        updated = self.blackboard.update(
            run_id,
            facts=result.facts,
            hypotheses=result.hypotheses,
            evidence_refs=result.evidence_refs,
            event={
                "type": "worker.observed",
                "action": intent.action,
                "status": result.status,
            },
        )
        return CoordinatorStep("CONTINUE", updated, intent=intent, result=result)
