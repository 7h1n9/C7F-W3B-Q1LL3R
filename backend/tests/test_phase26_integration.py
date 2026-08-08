from __future__ import annotations

from typing import Any

import pytest

from app.solver.blackboard import BlackboardState
from app.solver.blackboard.repository import apply_patch
from app.solver.classification import LLMClassifierConfig, LLMVulnerabilityClassifier
from app.solver.context import ChallengeContext, TargetContext
from app.solver.loop import SolverLoop
from app.solver.planner import DeterministicPlanner
from app.solver.policy import ActionPolicyValidator
from app.solver.state_machine import TaskStateMachine
from app.solver.worker import MockWorker, WorkerManager


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


class FakeClassifierClient:
    async def __call__(self, request: dict[str, Any], **_: Any) -> dict[str, Any]:
        return {
            "hypotheses": [
                {
                    "type": "SQLInjection",
                    "confidence": 0.85,
                    "reason": "query parameter signal",
                }
            ]
        }


@pytest.mark.asyncio
async def test_classifier_blackboard_planner_chain() -> None:
    state = BlackboardState(
        run_id="phase26-chain",
        phase="BASELINE",
        knowledge={"target_url": "http://target.test/search"},
    )
    repository = MemoryRepository(state)
    context = ChallengeContext(
        challenge_id="phase26",
        title="Asset warranty",
        description="Search an asset by asset_no.",
        target=TargetContext("http://target.test/search", ("target.test",), "WEB_TARGET"),
        objective="Find the vulnerability.",
    )
    classifier = LLMVulnerabilityClassifier(
        config=LLMClassifierConfig(timeout_seconds=1),
        client=FakeClassifierClient(),
    )
    loop = SolverLoop(
        repository,
        state_machine=TaskStateMachine(),
        planner=DeterministicPlanner(),
        policy=ActionPolicyValidator(),
        worker_manager=WorkerManager(workers={"mock": MockWorker()}),
        classifier=classifier,
        challenge_context=context,
    )

    step = await loop.step(state.run_id)
    stored = await repository.load(state.run_id)

    assert stored is not None
    assert stored.vulnerability_hypotheses[0]["type"] == "SQLInjection"
    assert step.intent is not None
    assert step.intent.metadata["vulnerability_type"] == "SQLInjection"
