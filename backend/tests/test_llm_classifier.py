from __future__ import annotations

from typing import Any

import pytest

from app.solver.blackboard import BlackboardState
from app.solver.blackboard.repository import apply_patch
from app.solver.classification import (
    LLMClassifierConfig,
    LLMVulnerabilityClassifier,
    VulnerabilityClassifier,
)
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


def context() -> ChallengeContext:
    return ChallengeContext(
        challenge_id="llm-classifier-test",
        title="Asset warranty",
        description="Search an asset by asset_no.",
        target=TargetContext("http://target.test/search", ("target.test",), "WEB_TARGET"),
        objective="Find the authorized vulnerability.",
    )


class FakeLLM:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.requests: list[dict[str, Any]] = []

    async def __call__(self, request: dict[str, Any], **_: Any) -> Any:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response


def make_classifier(client: Any, *, fallback: bool = True) -> LLMVulnerabilityClassifier:
    return LLMVulnerabilityClassifier(
        config=LLMClassifierConfig(
            use_llm=True,
            timeout_seconds=1,
            fallback_to_heuristic=fallback,
        ),
        client=client,
    )


@pytest.mark.asyncio
async def test_llm_response_is_parsed_and_normalized() -> None:
    client = FakeLLM(
        {
            "hypotheses": [
                {
                    "type": "SQL_INJECTION",
                    "confidence": 0.91,
                    "reason": "parameter reaches a query",
                    "evidence_refs": ["ev-1"],
                }
            ]
        }
    )

    result = await make_classifier(client).classify(context(), {"parameters": {"asset_no": "1"}})

    assert result == [
        {
            "type": "SQLInjection",
            "confidence": 0.91,
            "reason": "parameter reaches a query",
            "evidence_refs": ["ev-1"],
            "tested": False,
            "failed_attempts": 0,
        }
    ]
    assert client.requests[0]["messages"]


@pytest.mark.asyncio
async def test_llm_failure_falls_back_to_heuristic_classifier() -> None:
    classifier = make_classifier(FakeLLM(error=TimeoutError("provider timeout")))

    result = await classifier.classify(context(), {"parameters": {"asset_no": "1"}})

    assert result
    assert result[0]["type"] == "SQLInjection"
    assert classifier.last_source == "heuristic"


@pytest.mark.asyncio
async def test_planner_classify_task_prefers_llm_before_heuristic() -> None:
    llm = FakeLLM(
        {
            "hypotheses": [
                {"type": "FileUpload", "confidence": 0.88, "reason": "multipart form"}
            ]
        }
    )
    heuristic = VulnerabilityClassifier()
    planner = DeterministicPlanner(
        classifier=heuristic,
        llm_classifier=make_classifier(llm),
    )

    result = await planner._classify_task(context(), {"parameters": {"asset_no": "1"}})

    assert result[0]["type"] == "FileUpload"
    assert llm.requests


@pytest.mark.asyncio
async def test_classification_result_is_written_to_blackboard() -> None:
    state = BlackboardState(
        run_id="llm-loop-test",
        phase="BASELINE",
        knowledge={"target_url": "http://target.test/search"},
    )
    repository = MemoryRepository(state)
    classifier = make_classifier(
        FakeLLM(
            {
                "hypotheses": [
                    {"type": "SQLInjection", "confidence": 0.84, "reason": "query parameter"}
                ]
            }
        )
    )
    loop = SolverLoop(
        repository,
        state_machine=TaskStateMachine(),
        planner=DeterministicPlanner(),
        policy=ActionPolicyValidator(),
        worker_manager=WorkerManager(workers={"mock": MockWorker()}),
        classifier=classifier,
        challenge_context=context(),
    )

    step = await loop.step(state.run_id)
    stored = await repository.load(state.run_id)

    assert stored is not None
    assert stored.vulnerability_hypotheses[0]["type"] == "SQLInjection"
    assert step.state.vulnerability_hypotheses == stored.vulnerability_hypotheses


def test_planner_selects_highest_confidence_hypothesis() -> None:
    planner = DeterministicPlanner()
    state = BlackboardState(
        run_id="confidence-test",
        phase="BASELINE",
        knowledge={"target_url": "http://target.test/"},
        vulnerability_hypotheses=[
            {"type": "FileUpload", "confidence": 0.61, "tested": False},
            {"type": "SQLInjection", "confidence": 0.92, "tested": False},
        ],
    )

    intent = planner.plan(state, ["http_request", "file_upload"])

    assert intent is not None
    assert intent.metadata["vulnerability_type"] == "SQLInjection"
