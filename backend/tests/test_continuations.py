from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.challenge import Challenge
from app.models.run import RunContinuation, SolveRun
from app.services.continuations import (
    CONTINUATION_COMPLETED,
    CONTINUATION_FAILED,
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


# --------------------------------------------------------------------------- #
# A failed recovery must not be permanent.
#
# The dedupe key is ``(kind, run_id, revision, phase)`` and the supervisor
# re-requests it on every sweep.  ``request`` used to return whatever row held the
# key -- including a FAILED one -- and ``claim`` only takes PENDING, so the first
# failure made every later request a no-op.  The two zombie Runs in the database
# were exactly this: ``RUN_INVALID_STATE`` had failed their CHECKPOINT_RECOVERY,
# and the sweep then reported them as "being recovered" indefinitely.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_failed_continuation_is_rearmed_not_returned_dead(continuation_session):
    session, run = continuation_session
    item = await continuation_service.request(
        session, run.id, kind="CHECKPOINT_RECOVERY", dedupe_key="checkpoint:rea"
    )
    await session.commit()
    claimed = await continuation_service.claim(session, item.id)
    assert claimed is not None
    await continuation_service.fail(session, item.id, RuntimeError("RUN_INVALID_STATE"))

    persisted = await session.get(RunContinuation, item.id)
    assert persisted.status == CONTINUATION_FAILED

    # The very same request (same key, same revision) must produce an actionable
    # continuation again -- and the same row, so history is not duplicated.
    again = await continuation_service.request(
        session, run.id, kind="CHECKPOINT_RECOVERY", dedupe_key="checkpoint:rea"
    )
    await session.commit()
    assert again.id == item.id
    assert again.status == CONTINUATION_PENDING
    assert again.owner_instance_id is None
    assert again.claimed_at is None
    # Backed off, not immediate: the sweep runs every 5s and a permanently broken
    # Run must not be re-dispatched at that cadence.
    assert again.available_at > datetime.now(UTC)
    # The failed attempt is still on the record.
    assert again.attempts == 1
    assert again.last_error_code == "RuntimeError"


@pytest.mark.asyncio
async def test_an_active_continuation_still_dedupes(continuation_session):
    """Re-arming is for FAILED rows only; a queued one must stay a single row."""
    session, run = continuation_session
    first = await continuation_service.request(
        session, run.id, kind="CHECKPOINT_RECOVERY", dedupe_key="checkpoint:dup"
    )
    await session.commit()
    pending = await session.get(RunContinuation, first.id)
    before = pending.available_at

    second = await continuation_service.request(
        session, run.id, kind="CHECKPOINT_RECOVERY", dedupe_key="checkpoint:dup"
    )
    await session.commit()
    assert second.id == first.id
    assert second.status == CONTINUATION_PENDING
    assert second.available_at == before, "an already-queued continuation is not re-scheduled"

    count = await session.scalar(
        select(func.count()).select_from(RunContinuation).where(RunContinuation.run_id == run.id)
    )
    assert count == 1

