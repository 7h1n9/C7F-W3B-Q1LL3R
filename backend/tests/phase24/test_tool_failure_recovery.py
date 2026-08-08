from __future__ import annotations

import pytest

from app.models.run import SolveRun
from app.solver.service import SolverRuntimeService

from .conftest import FakeRunnerClient, make_run


@pytest.mark.asyncio
async def test_failed_runner_result_is_recorded_as_solver_feedback(session_factory) -> None:
    runner = FakeRunnerClient({"status": "FAILED", "error_code": "TARGET_UNAVAILABLE"})
    async with session_factory() as session:
        run = await make_run(session, max_steps=2, max_tools=2)
        result = await SolverRuntimeService(runner_client=runner).run(session, run.id)
        stored = await session.get(SolveRun, run.id)

    assert result.status == "COMPLETED_UNSOLVED"
    assert stored is not None
    history = stored.recovery_checkpoint_json["solver_blackboard"]["history"]
    assert any(item["type"] == "ACTION_FAILED" for item in history)
    assert stored.last_error_code == "MAX_AGENT_STEPS_REACHED"
