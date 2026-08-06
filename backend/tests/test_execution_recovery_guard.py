from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy import select

from app.models import Base
from app.models.challenge import Challenge
from app.models.multi_agent import AgentTask
from app.models.run import RunAttempt, RunEvent, RunExecutionLease, SolveRun, ToolCall
from app.orchestration.multi_agent_orchestrator import multi_agent_orchestrator
from app.services.execution_recovery import execution_recovery_guard
from app.services.run_attempts import RunAttemptService


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def make_run(session):
    challenge = Challenge(
        name="asset",
        target_url="http://asset.local",
        allowed_hosts=["asset.local"],
        challenge_type="WEB_TARGET",
    )
    session.add(challenge)
    await session.flush()
    run = SolveRun(
        challenge_id=challenge.id,
        workspace_path=".",
        role_snapshot_json={},
        status="EXECUTING",
        current_phase="BUSINESS_BASELINE",
    )
    session.add(run)
    await session.flush()
    return run


async def add_production_call(session, run, *, status="STARTED", runner_job_id="job-1"):
    now = datetime.now(UTC)
    attempt = RunAttempt(
        run_id=run.id,
        attempt_number=1,
        engine_type="mock",
        status="RUNNING",
        heartbeat_at=now,
    )
    task = AgentTask(
        run_id=run.id,
        agent_role="RECON",
        task_kind="RECON",
        objective="bounded production request",
        status="RUNNING",
        heartbeat_at=now,
        idle_deadline_at=now + timedelta(minutes=5),
        lease_expires_at=now + timedelta(minutes=5),
    )
    session.add_all([attempt, task])
    await session.flush()
    session.add(
        RunExecutionLease(
            run_id=run.id,
            attempt_id=attempt.id,
            owner_instance_id="test-owner",
            lease_token="test-lease-" + run.id,
            acquired_at=now,
            heartbeat_at=now,
            expires_at=now + timedelta(minutes=5),
        )
    )
    await session.flush()
    call = ToolCall(
        run_id=run.id,
        agent_role="RECON",
        agent_task_id=task.id,
        tool_name="http_request",
        arguments_json={},
        status=status,
        runner_job_id=runner_job_id,
        created_at=now - timedelta(minutes=10),
    )
    session.add(call)
    await session.flush()
    return call, task


@pytest.mark.asyncio
async def test_started_runner_job_is_protected_from_stale_recovery(session_factory):
    async with session_factory() as session:
        run = await make_run(session)
        call, _ = await add_production_call(session, run, runner_job_id="job-running")
        await session.commit()

        recoverable = await execution_recovery_guard.recoverable_tool_calls(
            session, run.id, stale_after_seconds=300
        )
        protected = await execution_recovery_guard.protected_production_calls(session, run.id)

        assert recoverable == []
        assert [item.id for item in protected] == [call.id]


@pytest.mark.asyncio
async def test_started_without_runner_job_is_recoverable(session_factory):
    async with session_factory() as session:
        run = await make_run(session)
        call, _ = await add_production_call(session, run, runner_job_id=None)
        await session.commit()

        recoverable = await execution_recovery_guard.recoverable_tool_calls(
            session, run.id, stale_after_seconds=300
        )

        assert [item.id for item in recoverable] == [call.id]


@pytest.mark.asyncio
async def test_planner_barrier_blocks_second_planner_during_production_call(session_factory):
    async with session_factory() as session:
        run = await make_run(session)
        await add_production_call(session, run, runner_job_id="job-running")
        await session.commit()

        allowed, reason = await multi_agent_orchestrator.can_dispatch_planner(session, run)

        assert allowed is False
        assert reason == "PRODUCTION_EXECUTION_ACTIVE"


@pytest.mark.asyncio
async def test_planner_barrier_opens_after_tool_completion(session_factory):
    async with session_factory() as session:
        run = await make_run(session)
        call, task = await add_production_call(session, run, status="COMPLETED")
        task.status = "COMPLETED"
        call.finished_at = datetime.now(UTC)
        await session.commit()

        allowed, reason = await multi_agent_orchestrator.can_dispatch_planner(session, run)

        assert allowed is True
        assert reason == "READY"


@pytest.mark.asyncio
async def test_planner_barrier_blocks_active_controller_task(session_factory):
    async with session_factory() as session:
        run = await make_run(session)
        session.add(
            AgentTask(
                run_id=run.id,
                agent_role="PLANNER",
                task_kind="PLANNING",
                objective="select next stage",
                status="RUNNING",
            )
        )
        await session.commit()

        allowed, reason = await multi_agent_orchestrator.can_dispatch_planner(session, run)

        assert allowed is False
        assert reason == "CONTROLLER_TASK_ACTIVE"


@pytest.mark.asyncio
async def test_recovery_does_not_replan_healthy_started_runner_call(session_factory):
    async with session_factory() as session:
        run = await make_run(session)
        call, task = await add_production_call(session, run, runner_job_id="job-running")
        task.idle_deadline_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

        changed = await RunAttemptService().recover_stale_execution(
            session, run, stale_after_seconds=300
        )

        assert changed is False
        await session.refresh(call)
        await session.refresh(task)
        assert call.status == "STARTED"
        assert task.status == "RUNNING"
        events = list((await session.scalars(select(RunEvent).where(RunEvent.run_id == run.id))).all())
        assert not any(event.event_type == "run.stale_execution_recovered" for event in events)


@pytest.mark.asyncio
async def test_recovery_allows_started_call_without_runner_job(session_factory):
    async with session_factory() as session:
        run = await make_run(session)
        call, task = await add_production_call(session, run, runner_job_id=None)
        task.idle_deadline_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

        changed = await RunAttemptService().recover_stale_execution(
            session, run, stale_after_seconds=300
        )

        assert changed is True
        await session.refresh(call)
        assert call.status == "FAILED"
