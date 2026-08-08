from __future__ import annotations

import pytest

from app.models.run import SolveRun
from app.solver.service import SolverRuntimeService

from .conftest import FakeRunnerClient, make_run


@pytest.mark.asyncio
async def test_runtime_timeout_terminates_the_solver_loop(session_factory) -> None:
    class SlowLoop:
        async def step(self, run_id):
            import asyncio

            await asyncio.sleep(0.2)

    runner = FakeRunnerClient()
    async with session_factory() as session:
        run = await make_run(session, runtime=0.01)
        result = await SolverRuntimeService(
            runner_client=runner,
            loop_factory=lambda **kwargs: SlowLoop(),
        ).run(session, run.id)
        stored = await session.get(SolveRun, run.id)

    assert result.status == "TIMEOUT"
    assert stored is not None and stored.status == "TIMEOUT"
    assert stored.last_error_code == "SOLVER_RUNTIME_TIMEOUT"
