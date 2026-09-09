from __future__ import annotations

import asyncio
import json

from app.solver.muteki import (
    EngineProfile,
    MutekiCoordinator,
    MutekiGraph,
    MutekiReason,
    MutekiWorkerPool,
)
from app.solver.muteki.worker import openai_compatible
from muteki.core.cost import CostController
from muteki.solver.cli_driver import CliResult


def test_multi_engine_race_assigns_complementary_lanes(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "race-lanes.sqlite", challenge_id="race-lanes")
    jobs = []

    async def external_runner(job):
        jobs.append(job)

    async def scenario() -> None:
        pool = MutekiWorkerPool(
            graph,
            lambda _job: None,
            max_workers=3,
            external_runner=external_runner,
        )
        coordinator = MutekiCoordinator(
            graph,
            MutekiReason(),
            pool,
            [EngineProfile("codex"), EngineProfile("openai"), EngineProfile("step")],
            config={"max_workers": 3, "max_ticks": 0},
        )
        await coordinator.run(max_ticks=0)

    asyncio.run(scenario())
    assert len(jobs) == 3
    assert len({job.route_hash for job in jobs}) == 3
    assert len({job.branch_id for job in jobs}) == 3
    assert len({job.engine_attempt_id for job in jobs}) == 3
    assert {job.payload["lane_key"] for job in jobs} == {
        "recon:public-endpoints",
        "recon:session-api",
        "recon:business-objects",
    }


def test_healthy_multi_engine_race_starts_all_engines_concurrently(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "race-concurrency.sqlite", challenge_id="race-concurrency")
    active = 0
    max_active = 0
    started: list[str] = []
    release = asyncio.Event()

    async def external_runner(job):
        nonlocal active, max_active
        started.append(job.engine_id)
        active += 1
        max_active = max(max_active, active)
        try:
            await release.wait()
        finally:
            active -= 1

    async def scenario() -> None:
        pool = MutekiWorkerPool(
            graph,
            lambda _job: None,
            max_workers=2,
            external_runner=external_runner,
        )
        coordinator = MutekiCoordinator(
            graph,
            MutekiReason(),
            pool,
            [EngineProfile("codex"), EngineProfile("openai-compatible:test")],
            config={"max_workers": 2},
        )
        race = asyncio.create_task(coordinator._race())
        for _ in range(50):
            if len(started) == 2:
                break
            await asyncio.sleep(0.01)
        assert started == ["codex", "openai-compatible:test"]
        assert max_active == 2
        release.set()
        await race

    asyncio.run(scenario())


def test_profile_selection_rotates_fairly(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "fair.sqlite", challenge_id="fair")
    pool = MutekiWorkerPool(graph, lambda _job: None, max_workers=1)
    coordinator = MutekiCoordinator(
        graph,
        MutekiReason(),
        pool,
        [EngineProfile("codex"), EngineProfile("openai"), EngineProfile("step")],
    )
    assert [coordinator._next_available_profile().engine_id for _ in range(3)] == [
        "codex",
        "openai",
        "step",
    ]


def test_openai_worker_returns_route_exhausted_for_visited_endpoint(tmp_path, monkeypatch) -> None:
    class Graph:
        def snapshot(self):
            return {
                "facts": [
                    {
                        "content": json.dumps(
                            {"tool": "http_request", "method": "GET", "endpoint": "/"}
                        )
                    }
                ],
                "intents": [],
                "dead_ends": [],
            }

        def add_evidence(self, **_kwargs):
            raise AssertionError("visited endpoint must not execute")

    called = False

    def run_command(*_args, **_kwargs):
        nonlocal called
        called = True
        return CliResult(text="{}")

    monkeypatch.setattr(openai_compatible, "run_cli_streaming_container", run_command)
    worker = openai_compatible.OpenAICompatibleWorker(
        graph=Graph(),
        container=object(),
        workspace=str(tmp_path),
        worker_id="openai-route",
        intent_id="intent-route",
        target_url="http://target.test",
        base_url="https://provider.test/v1",
        api_key="not-persisted",
        model="step-model",
        max_turns=1,
        timeout_seconds=10,
        cost=CostController(),
        run_id="run-route",
    )
    result = asyncio.run(
        worker._http("http_request", {"method": "GET", "url": "/"})
    )
    assert result["error"] == "ROUTE_EXHAUSTED"
    assert called is False
