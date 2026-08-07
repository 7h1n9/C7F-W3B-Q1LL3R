from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.security.action_authorizer import ActionSecurityDecision, SecurityDecisionType
from app.security.default_action_authorizer import DefaultActionAuthorizer
from app.solver.action import ActionIntent
from app.solver.blackboard import BlackboardState
from app.solver.blackboard.repository import apply_patch
from app.solver.context import RunContext, RunLimits, RuntimeUsage
from app.solver.context_factory import RunContextFactory
from app.solver.loop import SolverLoop
from app.solver.planner import DeterministicPlanner
from app.solver.policy import ActionPolicyValidator
from app.solver.state_machine import TaskStateMachine
from app.solver.worker import MockWorker, WorkerManager


def make_models(*, target_url: str = "http://target.test/search", allowed_hosts: list[str] | None = None, max_tool_calls: int = 4) -> tuple[Any, Any]:
    challenge = SimpleNamespace(
        id="challenge-23a",
        name="Scoped challenge",
        description="public description",
        challenge_type="WEB_TARGET",
        target_url=target_url,
        allowed_hosts=allowed_hosts if allowed_hosts is not None else ["target.test"],
        flag_pattern=r"flag\{secret\}",
        source_path="private/solution.json",
        metadata_json={
            "adapter": "web",
            "dbms": "mysql",
            "flag": "flag{secret}",
            "expected_answer": "secret",
            "solution": "hidden",
        },
    )
    run = SimpleNamespace(
        id="run-23a",
        max_agent_steps=20,
        max_tool_calls=max_tool_calls,
        max_runtime_seconds=900,
        solver_mode="multi_agent_v1",
        engine_type="mock",
        current_phase="INTAKE",
        secret="must not leak",
    )
    return challenge, run


def make_context(**kwargs: Any) -> RunContext:
    challenge, run = make_models(**kwargs)
    return RunContext.from_models(run, challenge)


def http_action(url: str = "http://target.test/search") -> ActionIntent:
    return ActionIntent("http_request", "phase 2.3a test", {"method": "GET", "url": url})


def sql_action(**parameters: Any) -> ActionIntent:
    return ActionIntent("sql_boolean_compare", "phase 2.3a test", parameters)


def test_context_factory_builds_standard_context_without_ground_truth() -> None:
    challenge, run = make_models()

    context = RunContextFactory().build(challenge, run)

    assert context.run_id == "run-23a"
    assert context.challenge.target.url == "http://target.test/search"
    assert context.limits == RunLimits(max_steps=20, max_actions=4, max_runtime_seconds=900.0)
    assert "flag" not in context.challenge.environment
    assert "solution" not in context.challenge.environment
    assert "secret" not in context.metadata


def test_context_factory_is_compatible_with_direct_mapper() -> None:
    challenge, run = make_models()

    assert RunContextFactory().build(challenge, run) == RunContext.from_models(run, challenge)


def test_context_factory_allows_explicit_static_policy_metadata_only() -> None:
    challenge, run = make_models()

    context = RunContextFactory(
        security_policy_id="web-default-v1",
        metadata={"deployment": "test"},
    ).build(challenge, run)

    assert context.security_policy_id == "web-default-v1"
    assert context.metadata["deployment"] == "test"


def test_default_policy_allows_in_scope_http_request() -> None:
    decision = DefaultActionAuthorizer().authorize(http_action(), make_context())

    assert decision.decision is SecurityDecisionType.ALLOW
    assert decision.reason_code == "ALLOW"


def test_default_policy_denies_unknown_capability() -> None:
    decision = DefaultActionAuthorizer().authorize(
        ActionIntent("shell_exec", "unsupported"),
        make_context(),
    )

    assert decision.decision is SecurityDecisionType.DENY
    assert decision.reason_code == "ACTION_NOT_ALLOWED"


def test_default_policy_denies_out_of_scope_url() -> None:
    decision = DefaultActionAuthorizer().authorize(
        http_action("http://attacker.test/search"),
        make_context(),
    )

    assert decision.reason_code == "TARGET_OUT_OF_SCOPE"


def test_default_policy_denies_hostname_suffix_bypass() -> None:
    decision = DefaultActionAuthorizer().authorize(
        http_action("http://target.test.attacker.test/search"),
        make_context(),
    )

    assert decision.decision is SecurityDecisionType.DENY
    assert decision.reason_code == "TARGET_OUT_OF_SCOPE"


def test_default_policy_denies_userinfo_bypass() -> None:
    decision = DefaultActionAuthorizer().authorize(
        http_action("http://target.test@attacker.test/search"),
        make_context(),
    )

    assert decision.decision is SecurityDecisionType.DENY
    assert decision.reason_code == "INVALID_TARGET"


def test_default_policy_allows_exact_host_and_port_scope() -> None:
    context = make_context(
        target_url="http://target.test:8080/search",
        allowed_hosts=["http://target.test:8080"],
    )

    allowed = DefaultActionAuthorizer().authorize(http_action("http://target.test:8080/search"), context)
    wrong_port = DefaultActionAuthorizer().authorize(http_action("http://target.test:8081/search"), context)

    assert allowed.decision is SecurityDecisionType.ALLOW
    assert wrong_port.reason_code == "TARGET_OUT_OF_SCOPE"


def test_default_policy_allows_explicit_ip_scope() -> None:
    context = make_context(
        target_url="http://127.0.0.1:8080/search",
        allowed_hosts=["127.0.0.1:8080"],
    )

    decision = DefaultActionAuthorizer().authorize(http_action("http://127.0.0.1:8080/search"), context)

    assert decision.decision is SecurityDecisionType.ALLOW


def test_default_policy_denies_invalid_url() -> None:
    decision = DefaultActionAuthorizer().authorize(http_action("http:///search"), make_context())

    assert decision.decision is SecurityDecisionType.DENY
    assert decision.reason_code == "INVALID_TARGET"


def test_sql_action_defaults_to_challenge_target() -> None:
    decision = DefaultActionAuthorizer().authorize(sql_action(test_field="id"), make_context())

    assert decision.decision is SecurityDecisionType.ALLOW


def test_default_policy_denies_when_tool_limit_is_reached() -> None:
    context = make_context(max_tool_calls=1)

    decision = DefaultActionAuthorizer().authorize_with_usage(
        http_action(),
        context,
        usage=RuntimeUsage(tool_calls=1),
    )

    assert decision.decision is SecurityDecisionType.DENY
    assert decision.reason_code == "TOOL_CALL_LIMIT_REACHED"


def test_default_policy_allows_tool_call_below_limit() -> None:
    decision = DefaultActionAuthorizer().authorize_with_usage(
        http_action(),
        make_context(max_tool_calls=2),
        RuntimeUsage(tool_calls=1),
    )

    assert decision.decision is SecurityDecisionType.ALLOW


class MemoryRepository:
    def __init__(self, state: BlackboardState) -> None:
        self.state = state

    async def load(self, run_id: str) -> BlackboardState | None:
        return self.state.copy_for_read() if self.state.run_id == run_id else None

    async def save(self, state: BlackboardState) -> BlackboardState:
        self.state = state.copy_for_read()
        return self.state.copy_for_read()

    async def update(self, run_id: str, patch: dict[str, Any], *, expected_version: int | None = None) -> BlackboardState:
        state = await self.load(run_id)
        assert state is not None
        if expected_version is not None:
            assert state.version == expected_version
        return await self.save(apply_patch(state, patch))


class ApprovalAuthorizer:
    def authorize(self, action: ActionIntent, context: RunContext | None) -> ActionSecurityDecision:
        return ActionSecurityDecision(
            SecurityDecisionType.REQUIRE_APPROVAL,
            reason="manual approval required",
            policy_id="test-approval",
        )


class CountingWorker(MockWorker):
    pass


async def test_require_approval_skips_worker_and_is_reduced_to_blackboard_feedback() -> None:
    worker = CountingWorker()
    state = BlackboardState(
        run_id="run-23a",
        phase="BASELINE",
        goal={"type": "SQL_INJECTION"},
        knowledge={"target_url": "http://target.test/search"},
    )
    loop = SolverLoop(
        MemoryRepository(state),
        state_machine=TaskStateMachine(),
        planner=DeterministicPlanner(),
        policy=ActionPolicyValidator(),
        worker_manager=WorkerManager(workers={"mock": worker}),
        action_authorizer=ApprovalAuthorizer(),
        run_context=make_context(),
    )

    step = await loop.step("run-23a")

    assert step.status == "APPROVAL_REQUIRED"
    assert step.event.event_type == "ACTION_APPROVAL_REQUIRED"
    assert step.result is not None and step.result.success is False
    assert worker.calls == []
    assert step.state.phase == "BASELINE"
    assert step.state.knowledge["hypotheses"][-1]["type"] == "ACTION_APPROVAL_REQUIRED"
    assert step.state.history[-1]["type"] == "ACTION_APPROVAL_REQUIRED"


async def test_default_policy_limit_is_enforced_before_worker_manager() -> None:
    worker = CountingWorker()
    state = BlackboardState(
        run_id="run-23a",
        phase="BASELINE",
        goal={"type": "SQL_INJECTION"},
        knowledge={"target_url": "http://target.test/search"},
        history=[{"type": "ACTION_COMPLETED", "action": "http_request"}],
    )
    loop = SolverLoop(
        MemoryRepository(state),
        state_machine=TaskStateMachine(),
        planner=DeterministicPlanner(),
        policy=ActionPolicyValidator(),
        worker_manager=WorkerManager(workers={"mock": worker}),
        action_authorizer=DefaultActionAuthorizer(),
        run_context=make_context(max_tool_calls=1),
    )

    step = await loop.step("run-23a")

    assert step.status == "DENIED"
    assert step.event.event_type == "ACTION_DENIED"
    assert step.result is not None
    assert step.result.output["status"] == "DENIED"
    assert worker.calls == []


async def test_default_policy_below_limit_allows_worker_manager() -> None:
    worker = CountingWorker()
    state = BlackboardState(
        run_id="run-23a",
        phase="BASELINE",
        goal={"type": "SQL_INJECTION"},
        knowledge={"target_url": "http://target.test/search"},
    )
    loop = SolverLoop(
        MemoryRepository(state),
        state_machine=TaskStateMachine(),
        planner=DeterministicPlanner(),
        policy=ActionPolicyValidator(),
        worker_manager=WorkerManager(workers={"mock": worker}),
        action_authorizer=DefaultActionAuthorizer(),
        run_context=make_context(max_tool_calls=2),
    )

    step = await loop.step("run-23a")

    assert step.status == "CONTINUE"
    assert worker.calls and worker.calls[0].action_name == "http_request"
