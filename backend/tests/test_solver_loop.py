from __future__ import annotations

from typing import Any

from app.solver.action import ActionIntent
from app.solver.blackboard import BlackboardState
from app.solver.blackboard.repository import apply_patch
from app.solver.loop import SolverLoop
from app.solver.planner import DeterministicPlanner
from app.solver.policy import ActionPolicyValidator
from app.solver.state_machine import TaskStateMachine
from app.solver.worker import MockWorker


class MemoryRepository:
    """Durable-repository-shaped store for loop tests; no real Run or DB."""

    def __init__(self, state: BlackboardState) -> None:
        self.state = state

    async def save(self, state: BlackboardState) -> BlackboardState:
        self.state = state.copy_for_read()
        return self.state.copy_for_read()

    async def load(self, run_id: str) -> BlackboardState | None:
        return self.state.copy_for_read() if self.state.run_id == run_id else None

    async def update(
        self,
        run_id: str,
        patch: dict[str, Any],
        *,
        expected_version: int | None = None,
    ) -> BlackboardState:
        current = await self.load(run_id)
        assert current is not None
        if expected_version is not None:
            assert current.version == expected_version
        return await self.save(apply_patch(current, patch))


def make_state(phase: str) -> BlackboardState:
    return BlackboardState(
        run_id="loop-test",
        phase=phase,
        goal={"type": "SQL_INJECTION"},
        knowledge={"target_url": "http://target/search"},
        control={},
    )


def make_loop(state: BlackboardState, worker: MockWorker | None = None) -> tuple[SolverLoop, MockWorker]:
    active_worker = worker or MockWorker()
    return (
        SolverLoop(
            MemoryRepository(state),
            state_machine=TaskStateMachine(),
            planner=DeterministicPlanner(),
            policy=ActionPolicyValidator(),
            worker=active_worker,
        ),
        active_worker,
    )


async def test_baseline_selects_http_and_updates_to_validation() -> None:
    loop, worker = make_loop(make_state("BASELINE"))

    step = await loop.step("loop-test")

    assert step.status == "CONTINUE"
    assert step.intent is not None
    assert step.intent.action_name == "http_request"
    assert step.event.event_type == "ACTION_COMPLETED"
    assert step.state.phase == "VALIDATION"
    assert step.state.knowledge["observations"][0]["observation"]["response"] == "xxx"
    assert len(worker.calls) == 1


async def test_validation_selects_sql_boolean_compare() -> None:
    loop, worker = make_loop(make_state("VALIDATION"))

    step = await loop.step("loop-test")

    assert step.intent is not None
    assert step.intent.action_name == "sql_boolean_compare"
    assert step.state.phase == "EXPLOITATION"
    assert len(worker.calls) == 1


class IllegalPlanner:
    def plan(self, state, allowed_actions):
        return ActionIntent("content_discovery", "illegal test action")


async def test_illegal_action_is_rejected_and_recorded_without_worker_call() -> None:
    worker = MockWorker()
    loop = SolverLoop(
        MemoryRepository(make_state("BASELINE")),
        state_machine=TaskStateMachine(),
        planner=IllegalPlanner(),
        policy=ActionPolicyValidator(),
        worker=worker,
    )

    step = await loop.step("loop-test")

    assert step.status == "REJECTED"
    assert step.event.event_type == "ACTION_REJECTED"
    assert step.state.history[-1]["type"] == "ACTION_REJECTED"
    assert worker.calls == []


async def test_worker_observation_and_event_history_are_written() -> None:
    loop, _ = make_loop(make_state("BASELINE"), MockWorker(response="observed"))

    step = await loop.step("loop-test")

    assert step.result is not None
    assert step.result.observation["response"] == "observed"
    assert step.state.evidence_refs == []
    assert step.state.history[-1]["payload"]["status"] == "SUCCESS"


async def test_two_steps_continue_from_baseline_to_validation_to_exploitation() -> None:
    loop, worker = make_loop(make_state("BASELINE"))

    first = await loop.step("loop-test")
    second = await loop.step("loop-test")

    assert first.state.phase == "VALIDATION"
    assert second.intent is not None
    assert second.intent.action_name == "sql_boolean_compare"
    assert second.state.phase == "EXPLOITATION"
    assert len(worker.calls) == 2
