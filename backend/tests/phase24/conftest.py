from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.challenge import Challenge
from app.models.run import SolveRun


class FakeRunnerClient:
    def __init__(self, result: dict | None = None, *, delay: float = 0.0) -> None:
        self.result = result or {"status": "COMPLETED", "status_code": 200, "body": "ok"}
        self.delay = delay
        self.create_calls: list[tuple] = []

    async def create_job(self, run_id, allowed_hosts, tool_name, arguments):
        self.create_calls.append((run_id, allowed_hosts, tool_name, arguments))
        return f"job-{len(self.create_calls)}"

    async def wait_job(self, job_id, **kwargs):
        import asyncio

        if self.delay:
            await asyncio.sleep(self.delay)
        return dict(self.result)


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def make_run(session, *, max_steps: int = 4, max_tools: int = 4, runtime: float = 30.0) -> SolveRun:
    challenge = Challenge(
        name="Phase 2.4 target fixture",
        description="A public target contract for deterministic integration tests.",
        challenge_type="WEB_TARGET",
        target_url="http://target.test/search",
        allowed_hosts=["target.test"],
        flag_pattern=r"flag\{[^}]+\}",
        source_path="",
        metadata_json={},
    )
    session.add(challenge)
    await session.flush()
    run = SolveRun(
        challenge_id=challenge.id,
        workspace_path=".",
        solver_mode="solver_v2",
        max_agent_steps=max_steps,
        max_tool_calls=max_tools,
        max_runtime_seconds=runtime,
    )
    session.add(run)
    await session.commit()
    return run
