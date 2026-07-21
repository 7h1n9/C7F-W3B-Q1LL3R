import asyncio
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.v1.ctfctl import ToolTicketRequest, tool_ticket
from app.core.config import get_settings
from app.models.base import Base
from app.models.run import RunAttempt, RunExecutionLease, SolveRun


@pytest.mark.asyncio
async def test_ten_tool_tickets_share_the_master_lease(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'tickets.db'}", poolclass=NullPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        run = SolveRun(challenge_id="challenge", workspace_path=str(tmp_path), status="EXECUTING", current_phase="EXECUTING")
        session.add(run)
        await session.flush()
        attempt = RunAttempt(run_id=run.id, attempt_number=1, engine_type="codex_sdk", status="RUNNING")
        session.add(attempt)
        await session.flush()
        from datetime import UTC, datetime, timedelta
        lease = RunExecutionLease(run_id=run.id, attempt_id=attempt.id, owner_instance_id="test", lease_token="master", acquired_at=datetime.now(UTC), heartbeat_at=datetime.now(UTC), expires_at=datetime.now(UTC) + timedelta(minutes=1))
        session.add(lease)
        await session.commit()

    async def issue(index: int) -> str:
        async with sessions() as session:
            result = await tool_ticket(
                ToolTicketRequest(run_id=run.id, current_attempt_id=attempt.id, thread_id=f"thread-{index}", model_turn_id=f"turn-{index}"),
                get_settings().ctfctl_internal_access_key,
                session,
            )
            return str(result["data"]["ticket"])

    tickets = await asyncio.gather(*(issue(index) for index in range(10)))
    assert len(set(tickets)) == 10
    async with sessions() as session:
        refreshed = await session.get(RunExecutionLease, lease.id)
        assert refreshed.lease_token == "master"
    await engine.dispose()
