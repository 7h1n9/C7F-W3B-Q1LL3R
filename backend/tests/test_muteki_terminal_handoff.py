import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.challenge import Challenge
from app.models.run import RunAttempt, RunEvent, SolveRun
from app.services.run_supervisor import RunSupervisor


async def _session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _create_run(session, *, status: str = "RUNNING") -> SolveRun:
    challenge = Challenge(
        name="Muteki terminal handoff",
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
        status=status,
        current_phase="COORDINATOR",
    )
    session.add(run)
    await session.flush()
    return run


def _terminal_event(*, flag_found: bool, sequence: int = 1) -> RunEvent:
    return RunEvent(
        run_id="",
        sequence=sequence,
        event_type="muteki.run_finished",
        payload_json={
            "muteki_event_type": "run_finished",
            "payload": {
                "reason": "FLAG_VERIFIED" if flag_found else "NO_VERIFIED_FLAG",
                "flag_found": flag_found,
                "verified_flag_count": 1 if flag_found else 0,
            },
        },
    )


@pytest.mark.asyncio
async def test_reconcile_native_terminal_marks_solved() -> None:
    engine, sessions = await _session_factory()
    async with sessions() as session:
        run = await _create_run(session)
        event = _terminal_event(flag_found=True)
        event.run_id = run.id
        session.add(event)
        await session.commit()

        outcome = await RunSupervisor().reconcile_muteki_native_terminal(session, run)

        assert outcome is not None
        assert outcome.status == "COMPLETED_SOLVED"
        await session.refresh(run)
        assert run.status == "COMPLETED_SOLVED"
        assert run.current_phase == "REPORTING"
        assert run.recovery_checkpoint_json["muteki_native_terminal"]["flag_found"] is True
        events = list(
            (await session.scalars(select(RunEvent).where(RunEvent.run_id == run.id))).all()
        )
        assert any(event.event_type == "run.muteki_terminal_reconciled" for event in events)
    await engine.dispose()


@pytest.mark.asyncio
async def test_reconcile_native_terminal_marks_unsolved() -> None:
    engine, sessions = await _session_factory()
    async with sessions() as session:
        run = await _create_run(session)
        event = _terminal_event(flag_found=False)
        event.run_id = run.id
        session.add(event)
        await session.commit()

        outcome = await RunSupervisor().reconcile_muteki_native_terminal(session, run)

        assert outcome is not None
        assert outcome.status == "COMPLETED_UNSOLVED"
        await session.refresh(run)
        assert run.status == "COMPLETED_UNSOLVED"
        assert run.current_phase == "REPORTING"
        assert run.recovery_checkpoint_json["muteki_native_terminal"]["flag_found"] is False
    await engine.dispose()


@pytest.mark.asyncio
async def test_reconcile_native_terminal_is_noop_without_terminal_event() -> None:
    engine, sessions = await _session_factory()
    async with sessions() as session:
        run = await _create_run(session)
        await session.commit()

        outcome = await RunSupervisor().reconcile_muteki_native_terminal(session, run)

        assert outcome is None
        await session.refresh(run)
        assert run.status == "RUNNING"
    await engine.dispose()


@pytest.mark.asyncio
async def test_reconcile_native_terminal_ignores_event_older_than_latest_attempt() -> None:
    engine, sessions = await _session_factory()
    async with sessions() as session:
        run = await _create_run(session)
        now = datetime.now(UTC)
        event = _terminal_event(flag_found=True)
        event.run_id = run.id
        event.created_at = now - timedelta(minutes=5)
        session.add(event)
        session.add(
            RunAttempt(
                run_id=run.id,
                attempt_number=1,
                engine_type="codex_cli",
                status="RUNNING",
                started_at=now,
                heartbeat_at=now,
            )
        )
        await session.commit()

        outcome = await RunSupervisor().reconcile_muteki_native_terminal(session, run)

        assert outcome is None
        await session.refresh(run)
        assert run.status == "RUNNING"
    await engine.dispose()


@pytest.mark.asyncio
async def test_continue_until_terminal_reconciles_before_native_replay() -> None:
    engine, sessions = await _session_factory()
    async with sessions() as session:
        run = await _create_run(session)
        event = _terminal_event(flag_found=False)
        event.run_id = run.id
        session.add(event)
        await session.commit()
        run_id = run.id

        supervisor = RunSupervisor()
        called: list[str] = []

        async def fail_native_replay(session_arg, run_arg):
            called.append(str(run_arg.id))
            raise AssertionError("native Muteki runtime must not replay after FINALIZE")

        supervisor._run_muteki = fail_native_replay
        outcome = await supervisor.continue_until_terminal(session, run_id)

        assert outcome.status == "COMPLETED_UNSOLVED"
        assert called == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_reap_terminal_muteki_containers_includes_running_orphan(monkeypatch) -> None:
    import muteki.solver.container_exec as container_exec

    calls: list[tuple[str, bool]] = []

    def fake_teardown(run_id: str, *, remove: bool = True) -> bool:
        calls.append((run_id, remove))
        return True

    monkeypatch.setattr(container_exec, "teardown_container", fake_teardown)
    run = SimpleNamespace(id="run-orphan", solver_mode="muteki", status="RUNNING")

    reaped = await RunSupervisor().reap_terminal_muteki_containers([run])

    assert reaped == 1
    assert calls == [("run-orphan", True)]


@pytest.mark.asyncio
async def test_reap_terminal_muteki_containers_skips_active_task(monkeypatch) -> None:
    import muteki.solver.container_exec as container_exec

    calls: list[str] = []

    def fake_teardown(run_id: str, *, remove: bool = True) -> bool:
        calls.append(run_id)
        return True

    monkeypatch.setattr(container_exec, "teardown_container", fake_teardown)
    supervisor = RunSupervisor()
    task = asyncio.create_task(asyncio.sleep(10))
    supervisor._active_run_tasks["run-active"] = task
    run = SimpleNamespace(id="run-active", solver_mode="muteki", status="RUNNING")
    try:
        reaped = await supervisor.reap_terminal_muteki_containers([run])
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert reaped == 0
    assert calls == []


@pytest.mark.asyncio
async def test_run_finished_event_carries_flag_outcome() -> None:
    from app.solver.muteki.coordinator import MutekiCoordinator, MutekiPhase
    from app.solver.muteki.events import EventType

    class FakeGraph:
        def __init__(self) -> None:
            self.events = []

        def flags(self, verified_only: bool = False):
            return [SimpleNamespace(flag_value="flag{test}")] if verified_only else []

        def emit_event(self, **kwargs):
            self.events.append(kwargs)

        def release_claims(self, actor: str = "") -> None:
            return None

    class FakePool:
        active_count = 0

        async def cancel_all(self) -> None:
            return None

    coordinator = object.__new__(MutekiCoordinator)
    coordinator._finalized = False
    coordinator._stop_reason = "FLAG_VERIFIED"
    coordinator.graph = FakeGraph()
    coordinator.official_worker = None
    coordinator._health_retry_task = None
    coordinator._reason_health_retry_task = None
    coordinator.pool = FakePool()
    coordinator._semantic_reservations = {}
    coordinator._active_route_hashes = set()
    coordinator.phase = MutekiPhase.COORDINATOR
    coordinator.stage_policy = SimpleNamespace(can_transition=lambda *_: True)
    coordinator._change_phase = lambda phase: setattr(coordinator, "phase", phase)

    await coordinator.finalize(reason="STOPPED")

    finished = next(
        event
        for event in coordinator.graph.events
        if event["event_type"] == EventType.RUN_FINISHED
    )
    assert finished["payload"]["flag_found"] is True
    assert finished["payload"]["verified_flag_count"] == 1
