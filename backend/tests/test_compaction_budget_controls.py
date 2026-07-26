from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.exceptions import DomainError
from app.models.base import Base
from app.models.challenge import Challenge
from app.models.run import LogicalToolCall, RunAttempt, RunExecutionLease, SolveRun
from app.schemas.compaction import CompactionDecisionAction
from app.services.compaction import compaction_service
from app.services.effective_logical_tool_calls import effective_logical_tool_call_service
from app.services.run_budget_guard import run_budget_guard


@pytest.fixture
async def session_factory(tmp_path: Path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_effective_logical_call_is_singleton(session_factory, tmp_path: Path) -> None:
    async with session_factory() as session:
        challenge = Challenge(name="demo", target_url="http://demo.local", allowed_hosts=["demo.local"], challenge_type="WEB_TARGET", flag_pattern=r"flag\{[^}]+\}")
        session.add(challenge)
        await session.flush()
        run = SolveRun(challenge_id=challenge.id, workspace_path=str(tmp_path), engine_type="codex_sdk")
        session.add(run)
        await session.flush()
        first = await effective_logical_tool_call_service.ensure(session, run, logical_tool_call_id="mcp:item-1", tool_name="http_request", arguments={"url": "http://demo.local"})
        second = await effective_logical_tool_call_service.ensure(session, run, logical_tool_call_id="mcp:item-1", tool_name="http_request", arguments={"url": "http://demo.local"})
        await session.commit()
        count = await session.scalar(select(func.count()).select_from(LogicalToolCall).where(LogicalToolCall.run_id == run.id))
    assert first.id == second.id
    assert count == 1


@pytest.mark.asyncio
async def test_budget_pauses_before_next_call(session_factory, tmp_path: Path) -> None:
    async with session_factory() as session:
        challenge = Challenge(name="demo", target_url="http://demo.local", allowed_hosts=["demo.local"], challenge_type="WEB_TARGET", flag_pattern=r"flag\{[^}]+\}")
        session.add(challenge)
        await session.flush()
        run = SolveRun(challenge_id=challenge.id, workspace_path=str(tmp_path), max_tool_calls=1, status="EXECUTING", current_phase="EXECUTING")
        session.add(run)
        await session.flush()
        attempt = RunAttempt(run_id=run.id, attempt_number=1, engine_type="mock", status="RUNNING")
        session.add(attempt)
        await session.flush()
        session.add(RunExecutionLease(run_id=run.id, attempt_id=attempt.id, owner_instance_id="test", lease_token="lease", acquired_at=run.created_at, heartbeat_at=run.created_at, expires_at=run.created_at))
        await session.flush()
        await effective_logical_tool_call_service.ensure(session, run, logical_tool_call_id="first", tool_name="file_read")
        await session.commit()
        with pytest.raises(DomainError) as error:
            await run_budget_guard.enforce(session, run, attempt_id=attempt.id)
        assert error.value.code == "RUN_MAX_TOOL_CALLS"
        assert run.status == "PAUSED_BUDGET"
        assert await session.scalar(select(RunExecutionLease).where(RunExecutionLease.run_id == run.id)) is None


@pytest.mark.asyncio
async def test_compaction_archives_and_restores_snapshot(session_factory, tmp_path: Path) -> None:
    async with session_factory() as session:
        challenge = Challenge(name="demo", target_url="http://demo.local", allowed_hosts=["demo.local"], challenge_type="WEB_TARGET", flag_pattern=r"flag\{[^}]+\}")
        session.add(challenge)
        await session.flush()
        run = SolveRun(challenge_id=challenge.id, workspace_path=str(tmp_path), status="EXECUTING", current_phase="EXECUTING")
        session.add(run)
        await session.flush()
        for index in range(20):
            await effective_logical_tool_call_service.ensure(session, run, logical_tool_call_id=f"call-{index}", tool_name="http_request")
        await session.commit()
        triggered, _ = await compaction_service.should_compact(session, run)
        assert triggered is True
        result = await compaction_service.apply(session, run, CompactionDecisionAction())
        restored = await compaction_service.restore_latest_snapshot(session, run.id)
    archive = Path(result["archive_path"])
    assert archive.joinpath("archive-manifest.json").is_file()
    assert result["manifest"]["restorable"] is True
    assert restored and restored["generation"] == 1
