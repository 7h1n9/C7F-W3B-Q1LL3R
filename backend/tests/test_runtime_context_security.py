from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.security.action_authorizer import (
    ActionSecurityDecision,
    AllowAllActionAuthorizer,
    SecurityDecisionType,
)
from app.solver.action import ActionIntent
from app.solver.blackboard import BlackboardState
from app.solver.blackboard.repository import apply_patch
from app.solver.context import RunContext, RunLimits
from app.solver.loop import SolverLoop
from app.solver.planner import DeterministicPlanner
from app.solver.policy import ActionPolicyValidator
from app.solver.state_machine import TaskStateMachine
from app.solver.worker import WorkerResult


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


class CountingWorkerManager:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, action: ActionIntent) -> WorkerResult:
        self.calls += 1
        return WorkerResult(
            success=True,
            action_name=action.action_name,
            output={"status_code": 200, "body": "ok"},
            metadata={"backend": "fake", "status": "SUCCESS"},
        )


class DenyAuthorizer:
    def __init__(self) -> None:
        self.calls: list[tuple[ActionIntent, RunContext | None]] = []

    def authorize(
        self,
        action: ActionIntent,
        context: RunContext | None,
    ) -> ActionSecurityDecision:
        self.calls.append((action, context))
        return ActionSecurityDecision(
            decision=SecurityDecisionType.DENY,
            reason="policy denied test action",
            policy_id="test-deny",
        )


class AllowRecordingAuthorizer:
    def __init__(self) -> None:
        self.calls: list[tuple[ActionIntent, RunContext | None]] = []

    def authorize(
        self,
        action: ActionIntent,
        context: RunContext | None,
    ) -> ActionSecurityDecision:
        self.calls.append((action, context))
        return ActionSecurityDecision(
            decision=SecurityDecisionType.ALLOW,
            reason="test action allowed",
            policy_id="test-allow",
        )


def make_state() -> BlackboardState:
    return BlackboardState(
        run_id="run-context-test",
        phase="BASELINE",
        goal={"type": "SQL_INJECTION"},
        knowledge={"target_url": "http://target/search"},
    )


def make_context() -> RunContext:
    challenge = SimpleNamespace(
        id="challenge-1",
        name="Asset Warranty",
        description="Public challenge description",
        challenge_type="WEB_TARGET",
        target_url="http://target",
        allowed_hosts=["target"],
        flag_pattern="flag\\{secret\\}",
        source_path="private/solution.json",
        metadata_json={
            "adapter": "asset_warranty",
            "dbms": "mysql",
            "flag": "flag{secret}",
            "expected_answer": "secret",
            "solution": "hidden solution",
        },
    )
    run = SimpleNamespace(
        id="run-context-test",
        max_agent_steps=7,
        max_tool_calls=4,
        max_runtime_seconds=45,
        solver_mode="multi_agent_v1",
        engine_type="mock",
        current_phase="INTAKE",
        secret="must not be copied",
    )
    return RunContext.from_models(run, challenge)


def test_challenge_context_is_solver_safe_and_does_not_expose_ground_truth() -> None:
    context = make_context().challenge

    assert context.challenge_id == "challenge-1"
    assert context.target.url == "http://target"
    assert context.environment["dbms"] == "mysql"
    for forbidden in (
        "flag",
        "expected_flag",
        "expected_answer",
        "answer",
        "ground_truth",
        "solution",
        "solution_steps",
        "reference_solution",
        "evaluation_script",
        "checker_secret",
        "hidden_metadata",
    ):
        assert not hasattr(context, forbidden)
        assert forbidden not in context.environment
        assert forbidden not in context.constraints


def test_run_context_contains_challenge_and_existing_run_limits() -> None:
    context = make_context()

    assert context.run_id == "run-context-test"
    assert context.challenge.challenge_id == "challenge-1"
    assert context.limits == RunLimits(
        max_steps=7,
        max_actions=4,
        max_runtime_seconds=45.0,
    )
    assert context.metadata["solver_mode"] == "multi_agent_v1"
    assert "secret" not in context.metadata


def test_default_authorizer_allows_existing_action() -> None:
    decision = AllowAllActionAuthorizer().authorize(
        ActionIntent("http_request", "baseline"),
        make_context(),
    )

    assert decision.decision is SecurityDecisionType.ALLOW
    assert decision.allowed is True


async def test_security_deny_does_not_call_worker_or_crash_loop() -> None:
    manager = CountingWorkerManager()
    authorizer = DenyAuthorizer()
    loop = SolverLoop(
        MemoryRepository(make_state()),
        state_machine=TaskStateMachine(),
        planner=DeterministicPlanner(),
        policy=ActionPolicyValidator(),
        worker_manager=manager,
        run_context=make_context(),
        action_authorizer=authorizer,
    )

    step = await loop.step("run-context-test")

    assert step.status == "DENIED"
    assert step.event.event_type == "ACTION_DENIED"
    assert step.result is not None
    assert step.result.success is False
    assert step.result.metadata["policy_id"] == "test-deny"
    assert manager.calls == 0
    assert step.state.phase == "BASELINE"
    assert step.state.history[-1]["type"] == "ACTION_DENIED"


async def test_security_allow_preserves_worker_manager_chain() -> None:
    manager = CountingWorkerManager()
    authorizer = AllowRecordingAuthorizer()
    context = make_context()
    loop = SolverLoop(
        MemoryRepository(make_state()),
        state_machine=TaskStateMachine(),
        planner=DeterministicPlanner(),
        policy=ActionPolicyValidator(),
        worker_manager=manager,
        run_context=context,
        action_authorizer=authorizer,
    )

    step = await loop.step("run-context-test")

    assert step.status == "CONTINUE"
    assert step.event.event_type == "ACTION_COMPLETED"
    assert manager.calls == 1
    assert authorizer.calls[0][0].action_name == "http_request"
    assert authorizer.calls[0][1] is context
    assert authorizer.calls[0][1].run_id == "run-context-test"
    assert authorizer.calls[0][1].challenge.challenge_id == "challenge-1"
    assert step.state.phase == "VALIDATION"
