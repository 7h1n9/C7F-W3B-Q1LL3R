from __future__ import annotations

from typing import Any

import pytest

from app.solver.action import ActionIntent
from app.solver.blackboard import BlackboardState
from app.solver.blackboard.repository import apply_patch
from app.solver.loop import SolverLoop
from app.solver.planner import DeterministicPlanner
from app.solver.policy import ActionPolicyValidator
from app.solver.state_machine import TaskStateMachine
from app.solver.worker import (
    MockWorker,
    RunnerWorker,
    WorkerManager,
    WorkerUnavailable,
)


class MemoryRepository:
    def __init__(self, state: BlackboardState) -> None:
        self.state = state

    async def load(self, run_id: str) -> BlackboardState | None:
        if self.state.run_id != run_id:
            return None
        return self.state.copy_for_read()

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


def make_state(phase: str = "BASELINE") -> BlackboardState:
    return BlackboardState(
        run_id="adapter-test",
        phase=phase,
        goal={"type": "SQL_INJECTION"},
        knowledge={"target_url": "http://target/search"},
    )


async def test_worker_manager_routes_mock_backend_to_mock_worker() -> None:
    worker = MockWorker(response="adapter-response")
    manager = WorkerManager(workers={"mock": worker})
    action = ActionIntent(
        action_name="http_request",
        reason="test mock route",
        metadata={"backend": "mock"},
    )

    result = await manager.execute(action)

    assert result.success is True
    assert result.action_name == "http_request"
    assert result.output["response"] == "adapter-response"
    assert worker.calls == [action]


async def test_unknown_backend_is_rejected_without_fallback() -> None:
    manager = WorkerManager(workers={"mock": MockWorker()})
    action = ActionIntent(
        action_name="http_request",
        reason="test unknown route",
        metadata={"backend": "unknown"},
    )

    with pytest.raises(WorkerUnavailable):
        await manager.execute(action)


async def test_runner_adapter_requires_run_context_without_calling_runner() -> None:
    manager = WorkerManager(workers={"runner": RunnerWorker()})
    action = ActionIntent(
        action_name="http_request",
        reason="runner placeholder",
        metadata={"backend": "runner"},
    )

    result = await manager.execute(action)

    assert result.success is False
    assert result.metadata["status"] == "INVALID_REQUEST"
    assert result.metadata["error_code"] == "RUN_ID_REQUIRED"


async def test_solver_loop_uses_worker_manager_and_reducer_chain() -> None:
    worker = MockWorker()
    loop = SolverLoop(
        MemoryRepository(make_state()),
        state_machine=TaskStateMachine(),
        planner=DeterministicPlanner(),
        policy=ActionPolicyValidator(),
        worker_manager=WorkerManager(workers={"mock": worker}),
    )

    step = await loop.step("adapter-test")

    assert step.state.phase == "VALIDATION"
    assert step.state.knowledge["verified_facts"]
    assert worker.calls[0].action_name == "http_request"
