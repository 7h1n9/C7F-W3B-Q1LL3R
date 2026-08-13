import asyncio
import importlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.challenge import Challenge
from app.models.run import RunAttempt, RunExecutionLease, SolveRun
from app.services.run_supervisor import RunSupervisor

run_supervisor_module = importlib.import_module("app.services.run_supervisor")


@pytest.mark.asyncio
async def test_muteki_attempt_heartbeat_renews_outer_lease(monkeypatch) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(run_supervisor_module, "SessionLocal", factory)

    now = datetime.now(UTC)
    async with factory() as session:
        challenge = Challenge(
            name="Muteki heartbeat test",
            description="test",
            challenge_type="WEB_TARGET",
            target_url="http://target.test/",
            allowed_hosts=["target.test"],
            flag_pattern=r"flag\{[^}]+\}",
        )
        session.add(challenge)
        await session.flush()
        run = SolveRun(
            challenge_id=challenge.id,
            workspace_path=".",
            solver_mode="muteki",
            status="RUNNING",
        )
        session.add(run)
        await session.flush()
        attempt = RunAttempt(
            run_id=run.id,
            attempt_number=1,
            engine_type="codex_sdk",
            status="RUNNING",
            started_at=now,
            heartbeat_at=now,
        )
        session.add(attempt)
        await session.flush()
        lease = RunExecutionLease(
            run_id=run.id,
            attempt_id=attempt.id,
            owner_instance_id="heartbeat-test",
            lease_token="heartbeat-test-token",
            acquired_at=now,
            heartbeat_at=now,
            expires_at=now + timedelta(seconds=60),
        )
        session.add(lease)
        await session.commit()
        run_id, attempt_id, lease_id = run.id, attempt.id, lease.id

    heartbeat = asyncio.create_task(
        RunSupervisor()._heartbeat_muteki_attempt(
            run_id=run_id,
            attempt_id=attempt_id,
            lease_id=lease_id,
            interval_seconds=0.01,
        )
    )
    await asyncio.sleep(0.15)
    heartbeat.cancel()
    await asyncio.gather(heartbeat, return_exceptions=True)

    async with factory() as session:
        stored_run = await session.get(SolveRun, run_id)
        stored_attempt = await session.get(RunAttempt, attempt_id)
        stored_lease = await session.get(RunExecutionLease, lease_id)
        assert stored_run is not None
        assert stored_attempt is not None
        assert stored_lease is not None
        assert stored_attempt.heartbeat_at.replace(tzinfo=UTC) > now
        assert stored_lease.heartbeat_at.replace(tzinfo=UTC) > now
        assert stored_lease.expires_at.replace(tzinfo=UTC) > now + timedelta(seconds=60)
        assert stored_run.updated_at.replace(tzinfo=UTC) > now

    await engine.dispose()
