from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.challenge import Challenge
from app.models.run import SolveRun
from app.schemas.run import RunCreate
from app.security.action_authorizer import ActionSecurityDecision, SecurityDecisionType
from app.security.default_action_authorizer import DefaultActionAuthorizer
from app.services.run_supervisor import RunSupervisor
from app.solver.action import ActionIntent
from app.solver.blackboard import BlackboardState, SolveRunBlackboardStore
from app.solver.context_factory import RunContextFactory
from app.solver.loop import SolverLoop
from app.solver.service import SolverRuntimeService


class FakeRunnerClient:
    def __init__(self, result: dict | None = None) -> None:
        self.result = result or {"status": "COMPLETED", "status_code": 200, "body": "ok"}
        self.create_calls: list[tuple] = []
        self.wait_calls: list[str] = []

    async def create_job(self, run_id, allowed_hosts, tool_name, arguments):
        self.create_calls.append((run_id, allowed_hosts, tool_name, arguments))
        return "job-1"

    async def wait_job(self, job_id, **kwargs):
        self.wait_calls.append(job_id)
        return dict(self.result)


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def make_run(session, *, max_steps: int = 1, max_tools: int = 1) -> SolveRun:
    challenge = Challenge(
        name="solver v2 test",
        description="public challenge description",
        challenge_type="WEB_TARGET",
        target_url="http://target.test/search",
        allowed_hosts=["target.test"],
        flag_pattern="private-flag-pattern",
        source_path="private/solution.json",
        metadata_json={"flag": "private-secret", "solution": "private-solution"},
    )
    session.add(challenge)
    await session.flush()
    run = SolveRun(
        challenge_id=challenge.id,
        workspace_path=".",
        solver_mode="solver_v2",
        max_agent_steps=max_steps,
        max_tool_calls=max_tools,
        max_runtime_seconds=30,
    )
    session.add(run)
    await session.commit()
    return run


def make_loop_factory(captured: dict):
    def factory(**kwargs):
        captured.update(kwargs)
        return SolverLoop(**kwargs)

    return factory


@pytest.mark.asyncio
async def test_production_service_uses_factory_default_policy_and_runner_path(session_factory) -> None:
    fake_runner = FakeRunnerClient()
    captured: dict = {}
    factory_calls: list[tuple] = []

    class SpyFactory(RunContextFactory):
        def build(self, challenge, run):
            factory_calls.append((challenge, run))
            return super().build(challenge, run)

    async with session_factory() as session:
        run = await make_run(session)
        result = await SolverRuntimeService(
            context_factory=SpyFactory(),
            runner_client=fake_runner,
            loop_factory=make_loop_factory(captured),
        ).run(session, run.id)

        stored = await session.get(SolveRun, run.id)
        assert result.status == "COMPLETED_UNSOLVED"
        assert stored.status == "COMPLETED_UNSOLVED"
        assert stored.started_at is not None
        assert stored.finished_at is not None
        assert len(factory_calls) == 1
        assert isinstance(captured["action_authorizer"], DefaultActionAuthorizer)
        assert fake_runner.create_calls[0][2] == "http_request"
        assert fake_runner.wait_calls == ["job-1"]
        checkpoint = stored.recovery_checkpoint_json["solver_blackboard"]
        assert checkpoint["knowledge"]["target_url"] == "http://target.test/search"
        assert "private-secret" not in str(checkpoint)


@pytest.mark.asyncio
async def test_out_of_scope_action_is_denied_before_runner(session_factory) -> None:
    fake_runner = FakeRunnerClient()

    class OutOfScopePlanner:
        def plan(self, state, allowed_actions):
            return ActionIntent(
                "http_request",
                "test out of scope",
                {"method": "GET", "url": "http://evil.test/search"},
            )

    async with session_factory() as session:
        run = await make_run(session)
        result = await SolverRuntimeService(
            planner_factory=lambda _context: OutOfScopePlanner(),
            runner_client=fake_runner,
        ).run(session, run.id)
        stored = await session.get(SolveRun, run.id)

        assert result.status == "COMPLETED_UNSOLVED"
        assert fake_runner.create_calls == []
        history = stored.recovery_checkpoint_json["solver_blackboard"]["history"]
        assert any(item["type"] == "ACTION_DENIED" for item in history)


@pytest.mark.asyncio
async def test_worker_failure_is_feedback_not_engine_failure(session_factory) -> None:
    fake_runner = FakeRunnerClient({"status": "FAILED", "error_code": "TARGET_UNAVAILABLE"})

    async with session_factory() as session:
        run = await make_run(session)
        result = await SolverRuntimeService(runner_client=fake_runner).run(session, run.id)
        stored = await session.get(SolveRun, run.id)

        assert result.status == "COMPLETED_UNSOLVED"
        assert stored.last_error_code == "MAX_AGENT_STEPS_REACHED"
        assert stored.last_error_code not in {"SOLVER_ENGINE_ERROR", "FAILED_RUNNER"}


@pytest.mark.asyncio
async def test_tool_limit_denies_second_worker_call(session_factory) -> None:
    fake_runner = FakeRunnerClient()

    async with session_factory() as session:
        run = await make_run(session, max_steps=2, max_tools=1)
        result = await SolverRuntimeService(runner_client=fake_runner).run(session, run.id)
        stored = await session.get(SolveRun, run.id)

        assert result.status == "COMPLETED_UNSOLVED"
        assert len(fake_runner.create_calls) == 1
        history = stored.recovery_checkpoint_json["solver_blackboard"]["history"]
        assert any(item["type"] == "ACTION_DENIED" for item in history)


@pytest.mark.asyncio
async def test_approval_feedback_does_not_call_worker(session_factory) -> None:
    fake_runner = FakeRunnerClient()

    class ApprovalAuthorizer:
        def authorize(self, action, context):
            return ActionSecurityDecision(
                decision=SecurityDecisionType.REQUIRE_APPROVAL,
                reason="manual approval required",
                policy_id="test-policy",
                reason_code="APPROVAL_REQUIRED",
            )

    async with session_factory() as session:
        run = await make_run(session)
        result = await SolverRuntimeService(
            authorizer_factory=lambda: ApprovalAuthorizer(),
            runner_client=fake_runner,
        ).run(session, run.id)
        stored = await session.get(SolveRun, run.id)

        assert result.status == "COMPLETED_UNSOLVED"
        assert fake_runner.create_calls == []
        history = stored.recovery_checkpoint_json["solver_blackboard"]["history"]
        assert any(item["type"] == "ACTION_APPROVAL_REQUIRED" for item in history)


@pytest.mark.asyncio
async def test_factory_failure_is_failed_engine_and_does_not_start_worker(session_factory) -> None:
    fake_runner = FakeRunnerClient()

    class BrokenFactory:
        def build(self, challenge, run):
            raise RuntimeError("private construction detail")

    async with session_factory() as session:
        run = await make_run(session)
        result = await SolverRuntimeService(
            context_factory=BrokenFactory(),
            runner_client=fake_runner,
        ).run(session, run.id)
        stored = await session.get(SolveRun, run.id)

        assert result.status == "FAILED_ENGINE"
        assert stored.status == "FAILED_ENGINE"
        assert stored.last_error_message == "Solver runtime failed before completing the run."
        assert fake_runner.create_calls == []


@pytest.mark.asyncio
async def test_unexpected_loop_exception_is_failed_engine(session_factory) -> None:
    class ExplodingLoop:
        async def step(self, run_id):
            raise RuntimeError("unexpected loop detail")

    async with session_factory() as session:
        run = await make_run(session)
        result = await SolverRuntimeService(
            loop_factory=lambda **kwargs: ExplodingLoop(),
        ).run(session, run.id)
        stored = await session.get(SolveRun, run.id)

        assert result.status == "FAILED_ENGINE"
        assert stored.status == "FAILED_ENGINE"
        assert stored.last_error_code == "SOLVER_ENGINE_ERROR"


@pytest.mark.asyncio
async def test_blackboard_store_preserves_other_checkpoint_keys(session_factory) -> None:
    async with session_factory() as session:
        run = await make_run(session)
        run.recovery_checkpoint_json = {"legacy_key": {"keep": True}}
        await session.commit()
        store = SolveRunBlackboardStore(session)
        state = await store.save(
            BlackboardState(
                run_id=run.id,
                phase="BASELINE",
                goal="test",
            )
        )
        loaded = await store.load(run.id)
        assert loaded == state
        refreshed = await session.get(SolveRun, run.id)
        assert refreshed.recovery_checkpoint_json["legacy_key"] == {"keep": True}


def test_run_create_accepts_solver_v2_without_changing_default() -> None:
    assert RunCreate().solver_mode == "multi_agent_v1"
    assert RunCreate(solver_mode="solver_v2").solver_mode == "solver_v2"


@pytest.mark.asyncio
async def test_supervisor_routes_only_explicit_solver_v2(monkeypatch) -> None:
    calls: list[tuple] = []

    class Session:
        async def get(self, model, run_id):
            return SimpleNamespace(solver_mode="solver_v2", id=run_id)

    class FakeService:
        async def run(self, session, run_id, user_message=None):
            calls.append((session, run_id, user_message))
            return SimpleNamespace(status="COMPLETED_UNSOLVED")

    import app.solver.service as service_module

    monkeypatch.setattr(service_module, "solver_runtime_service", FakeService())
    result = await RunSupervisor().continue_until_terminal(Session(), "run-v2", "ignored")

    assert result.status == "COMPLETED_UNSOLVED"
    assert calls[0][1:] == ("run-v2", "ignored")
