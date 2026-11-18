from __future__ import annotations

import asyncio

from app.solver.muteki import EngineProfile, MutekiGraph, MutekiReason
from app.solver.muteki.core.orchestrator import MutekiOrchestrator


def test_verified_flag_survives_run_timeout(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "timeout-flag.sqlite", challenge_id="challenge-timeout-flag")

    class TimedOutCoordinator:
        stop_reason = "MUTEKI_RUN_TIMEOUT"

        async def run(self, *, max_ticks: int | None = None, unbounded: bool = False) -> None:
            graph.write_flag(
                actor="worker-1",
                flag="flag{verified-before-timeout}",
                real_output="response body contains flag{verified-before-timeout}",
            )

    orchestrator = MutekiOrchestrator(
        graph,
        MutekiReason(),
        worker_runner=lambda _job: None,
        engines=[EngineProfile("gateway-runner")],
    )
    orchestrator.coordinator = TimedOutCoordinator()

    result = asyncio.run(orchestrator.run(max_rounds=1))

    assert result.status == "COMPLETED_SOLVED"
    assert result.flag_found is True
    assert result.flag == "flag{verified-before-timeout}"
    assert result.reason == "FLAG_VERIFIED_AFTER_TIMEOUT"
