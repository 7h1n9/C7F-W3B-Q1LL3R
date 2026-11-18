"""The outer RunSupervisor must not close a Muteki Run before Finalize.

``MutekiCoordinator.finalize`` (RUN_FINISHED, Worker cancellation, Sandbox
teardown) is the only authority that closes the solver loop.  These tests pin
the outer contract around it: a cancelled or timed-out Run is handed to a
terminal-status writer only once the Coordinator handoff has landed, and a
forced closure that exceeds both windows always leaves durable evidence.

No Docker, no model, no real Muteki runtime: the Coordinator lifecycle is a
fake with the same ``finally`` structure as
``app.solver.muteki.coordinator.MutekiCoordinator.run``.
"""

from __future__ import annotations

import asyncio
import importlib
import sys

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.challenge import Challenge
from app.models.run import RunEvent, SolveRun
from app.services.events import event_service
from app.services.run_supervisor import RunSupervisor

run_supervisor_module = importlib.import_module("app.services.run_supervisor")

MUTEKI_HANDOFF_EVENT = "muteki.run_finished"
MUTEKI_ACTIVITY_EVENT = "muteki.run.started"
GRACE_EXCEEDED_EVENT = "run.muteki_finalize_grace_exceeded"


class _ContainerExec:
    """Neutralize the official (Docker) container teardown."""

    @staticmethod
    def teardown_container(run_id: str, *, remove: bool = True) -> None:
        return None


@pytest.fixture
def container_exec(monkeypatch):
    monkeypatch.setitem(sys.modules, "muteki.solver.container_exec", _ContainerExec)


async def _session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


async def _create_run(factory, *, status: str = "RUNNING", max_total_runtime_seconds: int = 3600) -> str:
    async with factory() as session:
        challenge = Challenge(
            name="Muteki timeout finalize handoff",
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
            max_total_runtime_seconds=max_total_runtime_seconds,
        )
        session.add(run)
        await session.flush()
        run_id = run.id
        await session.commit()
    return run_id


async def _append_event(factory, run_id: str, event_type: str, payload: dict | None = None) -> None:
    async with factory() as session:
        await event_service.append(session, run_id, event_type, payload or {})


async def _event_types(factory, run_id: str) -> list[str]:
    async with factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(RunEvent.event_type)
                    .where(RunEvent.run_id == run_id)
                    .order_by(RunEvent.sequence)
                )
            ).all()
        )
    return [str(item) for item in rows]


async def _event_type_order(factory, run_id: str) -> list[tuple[int, str]]:
    async with factory() as session:
        rows = list(
            (
                await session.execute(
                    select(RunEvent.sequence, RunEvent.event_type)
                    .where(RunEvent.run_id == run_id)
                    .order_by(RunEvent.sequence)
                )
            ).all()
        )
    return [(int(sequence), str(event_type)) for sequence, event_type in rows]


class _FakeCoordinator:
    """Minimal Coordinator lifecycle: stop request -> ``finally`` Finalize."""

    def __init__(
        self,
        *,
        log: list[str],
        finalize_seconds: float,
        emit_handoff: bool = True,
        run_id: str | None = None,
        factory=None,
    ) -> None:
        self.log = log
        self.finalize_seconds = finalize_seconds
        self.emit_handoff = emit_handoff
        self.run_id = run_id
        self.factory = factory
        self.stop_reason: str | None = None
        self.finalized = False
        self._stop_requested = False

    def request_stop(self, reason: str = "STOP_REQUESTED") -> None:
        self._stop_requested = True
        self.stop_reason = str(reason)

    async def run(self, *, max_ticks: int | None = None, unbounded: bool = False) -> None:
        try:
            while not self._stop_requested:
                await asyncio.sleep(0.01)
        finally:
            await self.finalize(reason=self.stop_reason or "STOPPED")

    async def finalize(self, *, reason: str) -> None:
        if self.finalized:
            return
        self.log.append("finalize:start")
        await asyncio.sleep(self.finalize_seconds)
        if self.emit_handoff and self.run_id and self.factory is not None:
            await _append_event(
                self.factory,
                self.run_id,
                MUTEKI_HANDOFF_EVENT,
                {"payload": {"reason": reason, "flag_found": False}},
            )
        self.finalized = True
        self.log.append("finalize:done")


@pytest.mark.asyncio
async def test_cancel_waits_for_coordinator_finalize_handoff(monkeypatch, container_exec) -> None:
    """A cancel must not return (and terminalize) before Finalize completed."""

    engine, factory = await _session_factory()
    monkeypatch.setattr(run_supervisor_module, "SessionLocal", factory)
    run_id = await _create_run(factory)
    await _append_event(factory, run_id, MUTEKI_ACTIVITY_EVENT)
    log: list[str] = []

    async def outer_run() -> None:
        # Stands in for ``_run_muteki``: cancel enters the runtime/Coordinator
        # finally path, whose Finalize is slower than the cancel grace.
        coordinator = _FakeCoordinator(
            log=log,
            finalize_seconds=0.4,
            run_id=run_id,
            factory=factory,
        )
        try:
            await asyncio.sleep(60)
        finally:
            await coordinator.finalize(reason="STOP_REQUESTED")
            log.append("task:exited")

    supervisor = RunSupervisor()
    task = asyncio.create_task(outer_run())
    supervisor._active_run_tasks[run_id] = task
    await asyncio.sleep(0.05)

    try:
        handoff = await supervisor.cancel_muteki_with_handoff(
            run_id,
            grace_seconds=0.05,
            finalize_grace_seconds=2.0,
        )
        log.append("cancel:returned")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert handoff.had_active_task is True
    assert handoff.finalize_observed is True
    assert handoff.forced is False
    assert "finalize:done" in log
    # The terminal writer (POST /runs/{id}/cancel -> cancel_run_lifecycle) runs
    # only after cancel_muteki returns, so Finalize must already be complete.
    assert log.index("finalize:done") < log.index("cancel:returned")
    assert GRACE_EXCEEDED_EVENT not in await _event_types(factory, run_id)
    await engine.dispose()


@pytest.mark.asyncio
async def test_cancel_forced_closure_is_recorded_not_silent(monkeypatch, container_exec) -> None:
    """Exceeding every window still closes the Run - with durable evidence."""

    engine, factory = await _session_factory()
    monkeypatch.setattr(run_supervisor_module, "SessionLocal", factory)
    run_id = await _create_run(factory)
    await _append_event(factory, run_id, MUTEKI_ACTIVITY_EVENT)
    log: list[str] = []

    async def stuck_run() -> None:
        # A Finalize that never produces its RUN_FINISHED handoff.
        try:
            await asyncio.sleep(60)
        finally:
            log.append("finalize:start")
            await asyncio.sleep(60)

    supervisor = RunSupervisor()
    task = asyncio.create_task(stuck_run())
    supervisor._active_run_tasks[run_id] = task
    await asyncio.sleep(0.05)

    try:
        handoff = await supervisor.cancel_muteki_with_handoff(
            run_id,
            grace_seconds=0.05,
            finalize_grace_seconds=0.2,
        )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert handoff.had_active_task is True
    assert handoff.finalize_observed is False
    assert handoff.forced is True
    events = await _event_types(factory, run_id)
    assert GRACE_EXCEEDED_EVENT in events


@pytest.mark.asyncio
async def test_cancel_await_finalize_false_keeps_shutdown_semantics(monkeypatch, container_exec) -> None:
    """Service shutdown still cancels and reaps without judging Finalize."""

    engine, factory = await _session_factory()
    monkeypatch.setattr(run_supervisor_module, "SessionLocal", factory)
    run_id = await _create_run(factory)
    await _append_event(factory, run_id, MUTEKI_ACTIVITY_EVENT)
    log: list[str] = []

    async def outer_run() -> None:
        try:
            await asyncio.sleep(60)
        finally:
            await asyncio.sleep(0.3)
            log.append("task:exited")

    supervisor = RunSupervisor()
    task = asyncio.create_task(outer_run())
    supervisor._active_run_tasks[run_id] = task
    await asyncio.sleep(0.05)

    try:
        handoff = await supervisor.cancel_muteki_with_handoff(
            run_id,
            grace_seconds=1.0,
            await_finalize=False,
        )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert handoff.had_active_task is True
    assert handoff.task_stopped is True
    assert handoff.forced is False
    assert GRACE_EXCEEDED_EVENT not in await _event_types(factory, run_id)
    await engine.dispose()


@pytest.mark.asyncio
async def test_cancel_during_cooperative_timeout_wait_waits_for_finalize(
    monkeypatch, container_exec
) -> None:
    """Task exit is not proof of Finalize; the durable handoff is.

    This is the production interleaving behind "the Run ended before
    Finalize": the Run-level total timeout turns into ``request_stop`` and the
    Runtime waits for the Coordinator, then a stop request arrives.  The outer
    supervisor task therefore ends at that await while ``Coordinator.run``
    keeps finalizing in the background, so ``cancel_muteki`` must wait for the
    durable RUN_FINISHED handoff instead of treating "task finished" as
    "Finalize finished" and letting the caller close the Run right away.
    """

    engine, factory = await _session_factory()
    monkeypatch.setattr(run_supervisor_module, "SessionLocal", factory)
    run_id = await _create_run(factory)
    await _append_event(factory, run_id, MUTEKI_ACTIVITY_EVENT)
    log: list[str] = []
    background: list[asyncio.Task] = []

    async def outer_run() -> None:
        coordinator = _FakeCoordinator(
            log=log,
            finalize_seconds=0.5,
            run_id=run_id,
            factory=factory,
        )
        run_task = asyncio.create_task(coordinator.run(unbounded=True))
        background.append(run_task)
        try:
            await asyncio.wait_for(asyncio.shield(run_task), timeout=0.05)
        except asyncio.TimeoutError:
            log.append("runtime:deadline")
            coordinator.request_stop("MUTEKI_RUN_TIMEOUT")
            # Coordinate Finalize belongs to the Coordinator task, which keeps
            # running (shielded) even when this outer task is cancelled here.
            await asyncio.shield(run_task)
        log.append("task:exited")

    supervisor = RunSupervisor()
    task = asyncio.create_task(outer_run())
    supervisor._active_run_tasks[run_id] = task
    # The stop arrives while the outer task is inside the cooperative wait.
    await asyncio.sleep(0.1)
    assert not task.done()

    try:
        handoff = await supervisor.cancel_muteki_with_handoff(
            run_id,
            grace_seconds=1.0,
            finalize_grace_seconds=2.0,
        )
        log.append("cancel:returned")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for pending in background:
            if not pending.done():
                await asyncio.gather(pending, return_exceptions=True)

    assert handoff.had_active_task is True
    assert handoff.task_stopped is True
    assert handoff.finalize_observed is True
    assert handoff.forced is False
    assert log.index("finalize:done") < log.index("cancel:returned")
    assert GRACE_EXCEEDED_EVENT not in await _event_types(factory, run_id)
    await engine.dispose()


class _FakeRuntimeResult:
    def __init__(self, status: str, reason: str, flag_found: bool = False) -> None:
        self.status = status
        self.reason = reason
        self.flag_found = flag_found
        self.flag = None
        self.graph_path = ""


def _fake_runtime(*, log: list[str], emit_handoff: bool, factory, finalize_seconds: float = 0.05):
    class _FakeRuntime:
        def __init__(self, session, run, challenge) -> None:
            self.session = session
            self.run = run
            self.challenge = challenge
            self._run_id = str(run.id)

        async def run_once(self, *, max_rounds=None, max_workers=10) -> _FakeRuntimeResult:
            # Mirrors ``MutekiRuntime._run_orchestrator_with_deadline``: the
            # Run-level total timeout is a cooperative stop request and the
            # Finalize path is awaited, never cancelled.
            coordinator = _FakeCoordinator(
                log=log,
                finalize_seconds=finalize_seconds,
                emit_handoff=emit_handoff,
                run_id=self._run_id,
                factory=factory,
            )
            run_task = asyncio.create_task(coordinator.run(unbounded=True))
            try:
                await asyncio.wait_for(
                    asyncio.shield(run_task),
                    timeout=max(1, int(getattr(self.run, "max_total_runtime_seconds", 1) or 1)),
                )
                status = "COMPLETED_UNSOLVED"
                reason = "NO_VERIFIED_FLAG"
            except asyncio.TimeoutError:
                log.append("runtime:deadline")
                coordinator.request_stop("MUTEKI_RUN_TIMEOUT")
                await run_task
                status = "TIMEOUT"
                reason = "MUTEKI_RUN_TIMEOUT"
            log.append("runtime:returned")
            return _FakeRuntimeResult(status, reason)

    return _FakeRuntime


@pytest.mark.asyncio
async def test_timeout_run_becomes_terminal_only_after_finalize(monkeypatch) -> None:
    """The outer Run record must go terminal after the Coordinator handoff."""

    engine, factory = await _session_factory()
    monkeypatch.setattr(run_supervisor_module, "SessionLocal", factory)
    run_id = await _create_run(factory, max_total_runtime_seconds=1)
    await _append_event(factory, run_id, MUTEKI_ACTIVITY_EVENT)
    log: list[str] = []

    import app.orchestration.state_machine as state_machine

    original_transition = state_machine.transition

    def recording_transition(run, target, *args, **kwargs):
        log.append(f"transition:{getattr(target, 'value', target)}")
        return original_transition(run, target, *args, **kwargs)

    monkeypatch.setattr(state_machine, "transition", recording_transition)
    monkeypatch.setattr(
        importlib.import_module("app.solver.muteki.runtime"),
        "MutekiRuntime",
        _fake_runtime(log=log, emit_handoff=True, factory=factory),
    )

    supervisor = RunSupervisor()
    async with factory() as session:
        await supervisor.continue_until_terminal(session, run_id)

    async with factory() as session:
        run = await session.get(SolveRun, run_id)
    assert str(run.status) == "TIMEOUT"
    events = await _event_types(factory, run_id)
    assert MUTEKI_HANDOFF_EVENT in events
    assert GRACE_EXCEEDED_EVENT not in events
    event_order = await _event_type_order(factory, run_id)
    handoff_position = next(
        index
        for index, (_, event_type) in enumerate(event_order)
        if event_type == MUTEKI_HANDOFF_EVENT
    )
    failed_position = next(
        index
        for index, (_, event_type) in enumerate(event_order)
        if event_type == "run.failed"
    )
    assert handoff_position < failed_position
    # Finalize is the Coordinator's own terminal step and has to complete
    # before the outer lifecycle writes the terminal Run status.
    assert log.index("finalize:done") < log.index("runtime:returned")
    assert log.index("finalize:done") < log.index("transition:TIMEOUT")
    await engine.dispose()


@pytest.mark.asyncio
async def test_timeout_run_without_handoff_leaves_evidence(monkeypatch) -> None:
    """A runtime return without Finalize evidence is never a silent close."""

    engine, factory = await _session_factory()
    monkeypatch.setattr(run_supervisor_module, "SessionLocal", factory)
    run_id = await _create_run(factory, max_total_runtime_seconds=1)
    await _append_event(factory, run_id, MUTEKI_ACTIVITY_EVENT)
    log: list[str] = []
    monkeypatch.setattr(
        importlib.import_module("app.solver.muteki.runtime"),
        "MutekiRuntime",
        _fake_runtime(log=log, emit_handoff=False, factory=factory, finalize_seconds=0.0),
    )

    supervisor = RunSupervisor()
    async with factory() as session:
        await supervisor.continue_until_terminal(session, run_id)

    async with factory() as session:
        run = await session.get(SolveRun, run_id)
    assert str(run.status) == "TIMEOUT"
    events = await _event_types(factory, run_id)
    assert MUTEKI_HANDOFF_EVENT not in events
    async with factory() as session:
        evidence = await session.scalar(
            select(RunEvent).where(
                RunEvent.run_id == run_id,
                RunEvent.event_type == GRACE_EXCEEDED_EVENT,
            )
        )
    assert evidence is not None
    assert evidence.payload_json["reason"] == "RUNTIME_RETURNED_WITHOUT_HANDOFF"
    await engine.dispose()


@pytest.mark.asyncio
async def test_handoff_state_is_not_observable_for_unknown_runs(monkeypatch) -> None:
    """An unobservable Run never turns a cancellation into an indefinite wait."""

    engine, factory = await _session_factory()
    monkeypatch.setattr(run_supervisor_module, "SessionLocal", factory)
    supervisor = RunSupervisor()

    assert await supervisor._muteki_finalize_handoff_state("missing-run") == "unknown"
    assert await supervisor._await_muteki_finalize_handoff("missing-run", 5.0) is True
    await engine.dispose()
