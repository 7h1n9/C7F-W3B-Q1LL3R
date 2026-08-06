from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.challenge import Challenge
from app.models.run import RunContinuation, SolveRun
from app.services.continuations import (
    CONTINUATION_COMPLETED,
    CONTINUATION_PENDING,
    CONTINUATION_RUNNING,
    continuation_service,
)


@pytest.fixture
async def continuation_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        challenge = Challenge(name="continuation", target_url="http://target.test", allowed_hosts=["target.test"])
        session.add(challenge)
        await session.flush()
        run = SolveRun(challenge_id=challenge.id, workspace_path=".")
        session.add(run)
        await session.commit()
        yield session, run
    await engine.dispose()


@pytest.mark.asyncio
async def test_continuation_survives_claim_and_completion(continuation_session):
    session, run = continuation_session
    item = await continuation_service.request(
        session,
        run.id,
        kind="CHECKPOINT_RECOVERY",
        dedupe_key="checkpoint:1",
        payload={"phase": "BUSINESS_BASELINE"},
    )
    await session.commit()
    assert item.status == CONTINUATION_PENDING

    claimed = await continuation_service.claim(session, item.id)
    assert claimed is not None
    assert claimed["status"] == CONTINUATION_RUNNING
    assert claimed["run_id"] == run.id
    assert claimed["payload"] == {"phase": "BUSINESS_BASELINE"}

    await continuation_service.complete(session, item.id)
    persisted = await session.get(RunContinuation, item.id)
    assert persisted.status == CONTINUATION_COMPLETED


@pytest.mark.asyncio
async def test_stale_running_continuation_is_requeued(continuation_session):
    session, run = continuation_session
    item = RunContinuation(
        run_id=run.id,
        kind="CHECKPOINT_RECOVERY",
        dedupe_key="checkpoint:stale",
        status=CONTINUATION_RUNNING,
        payload_json={},
        claimed_at=datetime.now(UTC) - timedelta(minutes=10),
        available_at=datetime.now(UTC),
    )
    session.add(item)
    await session.commit()

    assert await continuation_service.recover_stale(session, stale_after_seconds=300) == 1
    persisted = await session.get(RunContinuation, item.id)
    assert persisted.status == CONTINUATION_PENDING
    assert persisted.owner_instance_id is None
