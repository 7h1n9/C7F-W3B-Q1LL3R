import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.challenge import Challenge
from app.models.run import RunEvent, RunUserInput, SolveRun
from app.services.events import event_service
from app.services.metadata_stage_decider import metadata_stage_decider
from app.services.run_finalizer import run_finalizer
from app.services.user_input_resume_guard import check_user_input_resume


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def test_metadata_fallback_blocks_database_and_selects_tables():
    decision = metadata_stage_decider.decide({
        "database": {"attempts": 2, "status": "BLOCKED"},
        "tables": {"status": "PENDING"},
        "columns": {"status": "PENDING"},
    })
    assert decision.stage == "tables"
    assert decision.target_expression == "information_schema.tables"


@pytest.mark.asyncio
async def test_user_input_resume_guard_rejects_terminal_without_pipeline(session_factory):
    async with session_factory() as session:
        challenge = Challenge(name="asset", target_url="http://asset.local", allowed_hosts=["asset.local"], challenge_type="WEB_TARGET")
        session.add(challenge)
        await session.flush()
        run = SolveRun(challenge_id=challenge.id, workspace_path=".", role_snapshot_json={}, status="WAITING_USER")
        session.add(run)
        await session.flush()
        session.add(RunUserInput(run_id=run.id, content="继续尝试", revision=1, status="CONSUMED"))
        await session.flush()
        await event_service.append(session, run.id, "user_input.consumed", {"input_id": "input-1"})
        guard = await check_user_input_resume(session, run.id)
        assert guard["ok"] is False
        await run_finalizer.finish_unsolved_with_wp(session, run, "NO_PROGRESS_LOOP")
        assert run.status == "WAITING_USER"
        assert run.last_error_code == "USER_INPUT_RESUME_NO_PROGRESS"


@pytest.mark.asyncio
async def test_user_input_resume_guard_accepts_followup_task_event(session_factory):
    async with session_factory() as session:
        challenge = Challenge(name="asset", target_url="http://asset.local", allowed_hosts=["asset.local"], challenge_type="WEB_TARGET")
        session.add(challenge)
        await session.flush()
        run = SolveRun(challenge_id=challenge.id, workspace_path=".", role_snapshot_json={}, status="PLANNING")
        session.add(run)
        await session.flush()
        await event_service.append(session, run.id, "user_input.consumed", {"input_id": "input-1"})
        await event_service.append(session, run.id, "agent.task.created", {"task_id": "planner-1", "agent_role": "PLANNER"})
        guard = await check_user_input_resume(session, run.id)
        assert guard["ok"] is True
