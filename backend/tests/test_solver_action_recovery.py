from __future__ import annotations

from typing import Any

import pytest

from app.solver.action import ActionIntent
from app.solver.action_lifecycle import (
    ActionExecutionRecord,
    ActionExecutionState,
    generate_fingerprint,
)
from app.solver.blackboard import BlackboardState
from app.solver.blackboard.repository import apply_patch
from app.solver.loop import SolverLoop
from app.solver.planner import DeterministicPlanner
from app.solver.policy import ActionPolicyValidator
from app.solver.state_machine import TaskStateMachine
from app.solver.worker import MockWorker, Worker, WorkerManager, WorkerResult


class MemoryRepository:
    def __init__(self, state: BlackboardState) -> None:
        self.state = state

    async def load(self, run_id: str) -> BlackboardState | None:
        return self.state.copy_for_read() if self.state.run_id == run_id else None

    async def save(self, state: BlackboardState) -> BlackboardState:
        self.state = state.copy_for_read()
        return self.state.copy_for_read()

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


class FailingWorker(Worker):
    async def execute(self, action: ActionIntent) -> WorkerResult:
        return WorkerResult(
            success=False,
            action_name=action.action_name,
            output={"error_code": "TARGET_UNAVAILABLE"},
            metadata={"backend": "mock", "status": "FAILED"},
        )


class ExplodingWorker(Worker):
    async def execute(self, action: ActionIntent) -> WorkerResult:
        raise RuntimeError("worker crashed")


class FixedPlanner:
    def __init__(self, intent: ActionIntent) -> None:
        self.intent = intent

    def plan(self, state: BlackboardState, allowed_actions: list[str]) -> ActionIntent:
        return self.intent


def make_state(*, control: dict[str, Any] | None = None) -> BlackboardState:
    return BlackboardState(
        run_id="action-recovery-test",
        phase="BASELINE",
        knowledge={"target_url": "http://target.test/search"},
        control=control or {},
    )


def make_loop(
    state: BlackboardState,
    worker: Worker | None = None,
    planner: object | None = None,
) -> tuple[SolverLoop, MemoryRepository, Worker | None]:
    repository = MemoryRepository(state)
    active_worker = worker or MockWorker()
    loop = SolverLoop(
        repository,
        state_machine=TaskStateMachine(),
        planner=planner or DeterministicPlanner(),
        policy=ActionPolicyValidator(),
        worker_manager=WorkerManager(workers={"mock": active_worker}),
    )
    return loop, repository, active_worker


def test_action_fingerprint_is_stable_for_equivalent_parameters() -> None:
    first = generate_fingerprint("http_request", {"b": 2, "a": 1})
    second = generate_fingerprint("http_request", {"a": 1, "b": 2})

    assert first == second
    assert first != generate_fingerprint("http_request", {"a": 2, "b": 1})


@pytest.mark.asyncio
async def test_action_started_is_persisted_before_worker_result() -> None:
    loop, repository, worker = make_loop(make_state())

    step = await loop.step("action-recovery-test")

    assert len(worker.calls) == 1  # type: ignore[union-attr]
    started = next(item for item in repository.state.history if item["type"] == "ACTION_STARTED")
    execution = started["payload"]["execution"]
    assert execution["state"] == ActionExecutionState.STARTED
    assert execution["action_id"]
    assert execution["fingerprint"]
    assert step.state.control["last_action_execution"]["state"] == "COMPLETED"
    assert step.state.control["active_action"] is None


@pytest.mark.asyncio
async def test_success_transitions_started_to_completed() -> None:
    loop, repository, _ = make_loop(make_state())

    await loop.step("action-recovery-test")

    execution_events = [
        item for item in repository.state.history if item["type"] in {"ACTION_STARTED", "ACTION_COMPLETED"}
    ]
    assert [item["payload"]["execution"]["state"] for item in execution_events] == [
        "STARTED",
        "COMPLETED",
    ]
    assert repository.state.control["last_action_execution"]["state"] == "COMPLETED"
    assert repository.state.control["active_action"] is None


@pytest.mark.asyncio
async def test_worker_failure_transitions_started_to_failed() -> None:
    loop, repository, _ = make_loop(make_state(), FailingWorker())

    step = await loop.step("action-recovery-test")

    assert step.event.event_type == "ACTION_FAILED"
    assert [
        item["payload"]["execution"]["state"]
        for item in repository.state.history
        if item["type"] in {"ACTION_STARTED", "ACTION_FAILED"}
    ] == ["STARTED", "FAILED"]


@pytest.mark.asyncio
async def test_worker_exception_transitions_started_to_interrupted() -> None:
    loop, repository, _ = make_loop(make_state(), ExplodingWorker())

    with pytest.raises(RuntimeError, match="worker crashed"):
        await loop.step("action-recovery-test")

    assert repository.state.history[-1]["type"] == "ACTION_INTERRUPTED"
    assert repository.state.history[-1]["payload"]["execution"]["state"] == "INTERRUPTED"
    assert repository.state.control["recovery_feedback"]["requires_explicit_retry"] is True


@pytest.mark.asyncio
async def test_started_action_is_interrupted_on_recovery_without_worker_call() -> None:
    action = ActionIntent("http_request", "recover", {"url": "http://target.test/search"})
    started = ActionExecutionRecord.pending(action).started()
    loop, repository, worker = make_loop(
        make_state(control={"active_action": started.to_dict()}),
    )

    step = await loop.step("action-recovery-test")

    assert step.status == "RECOVERY_REQUIRED"
    assert step.event.event_type == "ACTION_INTERRUPTED"
    assert step.state.control["active_action"] is None
    assert step.state.control["recovery_feedback"]["action_id"] == started.action_id
    assert worker.calls == []  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_same_fingerprint_cannot_silently_retry_after_recovery() -> None:
    action = ActionIntent("http_request", "recover", {"url": "http://target.test/search"})
    started = ActionExecutionRecord.pending(action).started()
    loop, repository, worker = make_loop(
        make_state(control={"active_action": started.to_dict()}),
    )

    await loop.step("action-recovery-test")
    retry_loop, _, retry_worker = make_loop(repository.state, planner=FixedPlanner(action))
    retry_step = await retry_loop.step("action-recovery-test")

    assert retry_step.status == "RECOVERY_REQUIRED"
    assert retry_step.event.event_type == "ACTION_RETRY_REJECTED"
    assert worker.calls == []  # type: ignore[union-attr]
    assert retry_worker.calls == []  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_retry_requires_retry_of_and_can_then_execute_explicitly() -> None:
    action = ActionIntent("http_request", "recover", {"url": "http://target.test/search"})
    started = ActionExecutionRecord.pending(action).started()
    loop, repository, _ = make_loop(
        make_state(control={"active_action": started.to_dict()}),
    )
    await loop.step("action-recovery-test")

    retry = ActionIntent(
        "http_request",
        "explicit retry",
        {"url": "http://target.test/search"},
        retry_of=started.action_id,
    )
    retry_loop, _, retry_worker = make_loop(repository.state, planner=FixedPlanner(retry))
    step = await retry_loop.step("action-recovery-test")

    assert step.event.event_type == "ACTION_COMPLETED"
    assert len(retry_worker.calls) == 1  # type: ignore[union-attr]
    assert step.event.payload["execution"]["retry_of"] == started.action_id
