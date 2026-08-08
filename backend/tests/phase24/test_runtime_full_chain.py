from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.run import RunEvent, SolveRun
from app.solver.service import SolverRuntimeService

from .conftest import FakeRunnerClient, make_run


@pytest.mark.asyncio
async def test_solver_v2_runtime_projects_a_complete_safe_chain(session_factory) -> None:
    runner = FakeRunnerClient()
    async with session_factory() as session:
        run = await make_run(session)
        result = await SolverRuntimeService(runner_client=runner).run(session, run.id)
        stored = await session.get(SolveRun, run.id)
        events = list((await session.scalars(select(RunEvent).where(RunEvent.run_id == run.id))).all())

    assert result.status == "COMPLETED_UNSOLVED"
    assert stored is not None and stored.status == "COMPLETED_UNSOLVED"
    assert runner.create_calls
    event_types = {event.event_type for event in events}
    assert {"solver.run.started", "solver.action.started", "solver.observation.received", "solver.run.completed"} <= event_types
    assert stored.recovery_checkpoint_json["solver_blackboard"]["control"]["active_action"] is None
