from __future__ import annotations

from typing import Any

import pytest

from app.solver.blackboard import BlackboardState
from app.solver.blackboard.repository import apply_patch
from app.solver.classification import VulnerabilityClassifier
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


def challenge_context() -> ChallengeContext:
    return ChallengeContext(
        challenge_id="strategy-test",
        title="Asset warranty",
        description="Search an asset by asset_no and return the warranty record.",
        target=TargetContext("http://target.test/search", ("target.test",), "WEB_TARGET"),
        objective="Find the authorized vulnerability.",
    )


def hypotheses() -> list[dict[str, Any]]:
    return [
        {
            "type": "SQLInjection",
            "confidence": 0.9,
            "reason": "parameter and database signal",
            "evidence_refs": [],
            "tested": False,
            "failed_attempts": 0,
        },
        {
            "type": "FileUpload",
            "confidence": 0.8,
            "reason": "upload endpoint signal",
            "evidence_refs": [],
            "tested": False,
            "failed_attempts": 0,
        },
    ]


def test_classifier_returns_sql_highest_confidence() -> None:
    result = VulnerabilityClassifier().classify(
        challenge_context(),
        {
            "parameters": {"asset_no": "1"},
            "body": "SQL syntax error near database",
        },
    )

    assert result
    assert result[0]["type"] == "SQLInjection"
    assert result[0]["confidence"] > 0.3
    assert result[0]["tested"] is False


def test_planner_switches_to_file_upload_after_three_failed_sql_attempts() -> None:
    planner = DeterministicPlanner()
    state = BlackboardState(
        run_id="switch-test",
        phase="BASELINE",
        knowledge={"target_url": "http://target.test/"},
        vulnerability_hypotheses=hypotheses(),
    )

    for _ in range(3):
        state = planner.apply_feedback(state, success=False, new_evidence=False)

    sql = next(item for item in state.vulnerability_hypotheses if item["type"] == "SQLInjection")
    assert sql["tested"] is True
    assert state.control["active_vulnerability_type"] == "FileUpload"
    intent = planner.plan(state, ["http_request", "file_upload"])
    assert intent is not None
    assert intent.action_name == "file_upload"
    assert intent.metadata["vulnerability_type"] == "FileUpload"


def test_planner_uses_generic_fallback_when_all_hypotheses_fail() -> None:
    planner = DeterministicPlanner()
    state = BlackboardState(
        run_id="generic-test",
        phase="BASELINE",
        knowledge={"target_url": "http://target.test/"},
        vulnerability_hypotheses=[{**hypotheses()[0], "failed_attempts": 3}],
    )

    intent = planner.plan(state, ["http_request"])

    assert intent is not None
    assert intent.action_name == "http_request"
    assert intent.metadata["vulnerability_type"] == "GENERIC"


@pytest.mark.asyncio
async def test_hypotheses_persist_across_solver_loop_iterations() -> None:
    initial = BlackboardState(
        run_id="persistent-strategy-test",
        phase="BASELINE",
        knowledge={"target_url": "http://target.test/search"},
        control={},
    )
    repository = MemoryRepository(initial)
    loop = SolverLoop(
        repository,
        state_machine=TaskStateMachine(),
        planner=DeterministicPlanner(),
        policy=ActionPolicyValidator(),
        worker_manager=WorkerManager(workers={"mock": MockWorker()}),
        classifier=VulnerabilityClassifier(),
        challenge_context=challenge_context(),
        initial_response={"parameters": {"asset_no": "1"}},
    )

    first = await loop.step(initial.run_id)
    second = await loop.step(initial.run_id)

    assert first.state.vulnerability_hypotheses
    assert second.state.vulnerability_hypotheses == first.state.vulnerability_hypotheses
    assert sum(item.get("type") == "VULNERABILITY_CLASSIFIED" for item in second.state.history) == 1
    assert second.intent is not None
