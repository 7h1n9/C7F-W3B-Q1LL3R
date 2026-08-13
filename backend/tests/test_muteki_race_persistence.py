from __future__ import annotations

import asyncio
import time

from app.solver.muteki import (
    EngineProfile,
    EventType,
    MutekiCoordinator,
    MutekiGraph,
    MutekiReason,
    MutekiWorkerPool,
)
from app.solver.muteki.phases import MutekiPhase
from app.solver.muteki.worker.official_worker import OfficialWorkerAdapter, OfficialWorkerResult
from app.solver.muteki.workers import WorkerJob


def _upstream_challenge(challenge_id: str):
    return type(
        "Challenge",
        (),
        {
            "id": challenge_id,
            "name": challenge_id,
            "challenge_type": "WEB_TARGET",
            "description": "",
            "target_url": "http://target.test",
            "flag_pattern": r"flag\{[^}]+\}",
        },
    )()


def test_compatibility_race_is_one_bounded_scout_and_persists_before_coordinator(tmp_path) -> None:
    graph_path = tmp_path / "race-persistence.sqlite"
    graph = MutekiGraph(graph_path, challenge_id="race-persistence")
    seen_roles: list[str] = []
    closed = False

    async def runner(job: WorkerJob):
        seen_roles.append(job.role)
        graph.add_fact(
            actor=job.worker_id,
            content="Race discovered the shared endpoint",
            verified=True,
        )

    async def scenario() -> None:
        pool = MutekiWorkerPool(graph, runner, max_workers=3)
        coordinator = MutekiCoordinator(
            graph,
            MutekiReason(),
            pool,
            [EngineProfile("codex"), EngineProfile("openai-compatible:worker")],
        )
        await coordinator._race()

    try:
        asyncio.run(scenario())
        assert seen_roles == ["race"]
        graph.close()
        closed = True

        reopened = MutekiGraph(graph_path, challenge_id="race-persistence")
        try:
            facts = reopened.facts(verified_only=True)
            assert [fact.content for fact in facts] == [
                "Race discovered the shared endpoint"
            ]
        finally:
            reopened.close()
    finally:
        if not closed:
            graph.close()


def test_shared_official_worker_adapter_keeps_race_workers_independent(tmp_path) -> None:
    adapter = OfficialWorkerAdapter()
    active = 0
    max_active = 0

    def fake_execute(_job):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        time.sleep(0.03)
        active -= 1
        return OfficialWorkerResult(True, "COMPLETED", "codex")

    adapter._execute_sync = fake_execute  # type: ignore[method-assign]

    def job(worker_id: str) -> WorkerJob:
        return WorkerJob(
            worker_id=worker_id,
            role="race",
            engine_id="codex",
            graph_path=str(tmp_path / "graph.sqlite"),
            challenge_id="shared-adapter",
        )

    async def scenario() -> None:
        results = await asyncio.gather(
            adapter.execute(job("worker-1")),
            adapter.execute(job("worker-2")),
        )
        assert all(result.success for result in results)

    asyncio.run(scenario())
    # Official Muteki starts one isolated solver execution window per selected
    # Race engine.  The adapter shares only the run Sandbox/Blackboard, not a
    # process handle or an execution lock.
    assert max_active == 2


def test_official_race_does_not_preflight_a_model_turn(tmp_path) -> None:
    from app.solver.muteki.adapter.upstream_runtime_graph import UpstreamRuntimeGraph

    graph = UpstreamRuntimeGraph(
        tmp_path / "health-gate.sqlite",
        challenge=_upstream_challenge("health-gate"),
        challenge_id="health-gate-run",
    )
    calls: list[str] = []

    class FakeOfficialWorker:
        async def health(self, _profile, *, workspace: str) -> tuple[bool, str]:
            raise AssertionError("Race must not use a model-turn health gate")

        async def execute(self, _job):
            calls.append("execute")
            return OfficialWorkerResult(False, "FAILED", "codex")

    async def scenario() -> None:
        pool = MutekiWorkerPool(graph, lambda _job: None, max_workers=1)
        coordinator = MutekiCoordinator(
            graph,
            MutekiReason(),
            pool,
            [EngineProfile("codex")],
            config={"worker_backend": "upstream_container"},
            official_worker_adapter=FakeOfficialWorker(),
        )
        await coordinator._race()

    try:
        asyncio.run(scenario())
        assert calls == ["execute"]
    finally:
        graph.close()


def test_official_prepare_health_gate_filters_unhealthy_cli_profile(tmp_path) -> None:
    from app.solver.muteki.adapter.upstream_runtime_graph import UpstreamRuntimeGraph

    graph = UpstreamRuntimeGraph(
        tmp_path / "prepare-health.sqlite",
        challenge=_upstream_challenge("prepare-health"),
        challenge_id="prepare-health-run",
    )
    checked: list[str] = []
    executed: list[str] = []
    observed_events = []
    graph._subscriber = observed_events.append

    class FakeOfficialWorker:
        async def start(self, **_kwargs):
            return None

        async def health(self, profile, *, workspace: str):
            checked.append(profile.engine_id)
            return profile.engine_id == "claude", (
                "HEALTHY" if profile.engine_id == "claude" else "WORKER_HEALTHCHECK_FAILED"
            )

        async def execute(self, job):
            executed.append(job.engine_id)
            return OfficialWorkerResult(False, "FAILED", job.engine_id)

        async def cancel_active(self):
            return None

        async def stop(self):
            return None

    async def scenario() -> None:
        pool = MutekiWorkerPool(graph, lambda _job: None, max_workers=2)
        coordinator = MutekiCoordinator(
            graph,
            MutekiReason(),
            pool,
            [EngineProfile("codex"), EngineProfile("claude")],
            config={"worker_backend": "upstream_container"},
            official_worker_adapter=FakeOfficialWorker(),
        )
        await coordinator._prepare()
        assert coordinator._unavailable_engine_ids == {"codex"}
        assert checked == ["codex", "claude"]
        await coordinator._race()

    try:
        asyncio.run(scenario())
        assert executed == ["claude"]
        checked_events = [
            event for event in observed_events
            if event.event_type == EventType.PREPARE_ENGINE_CHECKED
        ]
        assert {event.payload["engine_id"] for event in checked_events} == {"codex", "claude"}
        assert next(event for event in checked_events if event.payload["engine_id"] == "codex").payload["healthy"] is False
    finally:
        graph.close()


def test_failed_cli_profile_is_retried_after_worker_round_and_reactivated(tmp_path) -> None:
    from app.solver.muteki.adapter.upstream_runtime_graph import UpstreamRuntimeGraph

    graph = UpstreamRuntimeGraph(
        tmp_path / "health-retry.sqlite",
        challenge=_upstream_challenge("health-retry"),
        challenge_id="health-retry-run",
    )
    checked: list[str] = []
    executed: list[str] = []
    observed_events = []
    graph._subscriber = observed_events.append

    class FakeOfficialWorker:
        async def start(self, **_kwargs):
            return None

        async def health(self, profile, *, workspace: str):
            del workspace
            checked.append(profile.engine_id)
            codex_checks = checked.count("codex")
            if profile.engine_id == "codex" and codex_checks == 1:
                return False, "WORKER_HEALTHCHECK_FAILED"
            return True, "HEALTHY"

        async def execute(self, job):
            executed.append(job.engine_id)
            return OfficialWorkerResult(False, "FAILED", job.engine_id)

        async def cancel_active(self):
            return None

        async def stop(self):
            return None

    async def scenario() -> None:
        pool = MutekiWorkerPool(graph, lambda _job: None, max_workers=2)
        coordinator = MutekiCoordinator(
            graph,
            MutekiReason(),
            pool,
            [EngineProfile("codex"), EngineProfile("claude")],
            config={"worker_backend": "upstream_container", "interval_seconds": 0},
            official_worker_adapter=FakeOfficialWorker(),
        )
        await coordinator._prepare()
        assert coordinator._unavailable_engine_ids == {"codex"}

        # The healthy selected engine completes the first round.  The
        # Coordinator must schedule Codex reactivation without blocking this
        # round or requiring a new Run.
        await coordinator._race()
        await asyncio.sleep(0.05)
        assert "codex" not in coordinator._unavailable_engine_ids
        assert checked.count("codex") >= 2
        assert executed == ["claude"]

        coordinator._change_phase(MutekiPhase.COORDINATOR)
        graph.propose_intent(actor="test", description="retry recovered route", payload={})
        await coordinator._dispatch_open_intent()
        await pool.wait()
        assert executed[-1] == "codex"

        await coordinator.finalize(reason="TEST")

    try:
        asyncio.run(scenario())
        retry_events = [
            event
            for event in observed_events
            if event.event_type == EventType.PREPARE_ENGINE_CHECKED
            and event.payload.get("retry") is True
        ]
        assert retry_events
        assert any(
            event.payload.get("engine_id") == "codex"
            and event.payload.get("reactivated") is True
            for event in retry_events
        )
    finally:
        graph.close()


def test_reason_model_is_reactivated_after_worker_round(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "reason-health.sqlite", challenge_id="reason-health-run")
    observed_events = []
    graph._subscriber = observed_events.append

    class RecoveringReason:
        needs_reactivation = True

        def __init__(self) -> None:
            self.calls = 0

        async def health(self) -> tuple[bool, str]:
            self.calls += 1
            self.needs_reactivation = False
            return True, "HEALTHY"

    reason_provider = RecoveringReason()

    async def scenario() -> None:
        pool = MutekiWorkerPool(graph, lambda _job: None, max_workers=1)
        coordinator = MutekiCoordinator(
            graph,
            MutekiReason(provider=reason_provider),
            pool,
            [EngineProfile("codex")],
        )
        await coordinator._wait_for_worker_round()
        await asyncio.sleep(0.05)
        assert reason_provider.calls == 1
        await coordinator.finalize(reason="TEST")

    try:
        asyncio.run(scenario())
        assert any(
            event.event_type == EventType.REASON_MODEL_REACTIVATED
            and event.payload["reactivated"] is True
            for event in observed_events
        )
    finally:
        graph.close()
