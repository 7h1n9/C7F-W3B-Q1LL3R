"""Executor-side guard for benchmark Runs.

A matrix drives Runs through ``run_supervisor``, which has no upper bound of its
own.  Two failures followed from that and are pinned here:

* stopping the CLI cancelled the loop before anything closed the in-flight Run,
  leaving it ``RUNNING`` (and its container alive) forever;
* a Run whose own deadline never fired hung the whole matrix in wall-clock.

The guard must close the Run through the official cancel path *and* write the
terminal row, because the CLI has no API endpoint to do the second half.
"""

import asyncio
import importlib
import types
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.benchmark.experiment import ExecutionResult, RealRunExecutor
from app.models import Base
from app.models.challenge import Challenge
from app.models.run import SolveRun

experiment_module = importlib.import_module("app.benchmark.experiment")


class _FakeSupervisor:
    """Minimal stand-in for ``RunSupervisor``.

    ``behaviour`` decides what ``run_background`` does; the cancel path records
    that it was reached so the test can assert the official handoff ran before
    the row was written.
    """

    def __init__(self, behaviour: str = "hang") -> None:
        self.behaviour = behaviour
        self.cancelled: list[str] = []

    async def run_background(self, run_id: str):
        if self.behaviour == "finish":
            return types.SimpleNamespace(status="COMPLETED_UNSOLVED", error_code=None)
        await asyncio.Event().wait()

    async def cancel_muteki_with_handoff(self, run_id: str) -> None:
        self.cancelled.append(run_id)


class _FinishingOnCancelSupervisor(_FakeSupervisor):
    """A Run that closes itself exactly as the guard decides to cancel it.

    This is the race the guard must lose gracefully: the Coordinator got its
    result, so the row must keep it instead of being rewritten as CANCELLED.
    """

    def __init__(self, factory) -> None:
        super().__init__("hang")
        self._factory = factory

    async def cancel_muteki_with_handoff(self, run_id: str) -> None:
        self.cancelled.append(run_id)
        async with self._factory() as session:
            stored = await session.get(SolveRun, run_id)
            stored.status = "COMPLETED_SOLVED"
            await session.commit()


@pytest.fixture
async def database(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'runs.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _running_run(factory) -> str:
    async with factory() as session:
        challenge = Challenge(
            name="guard",
            description="guard",
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
            started_at=datetime.now(UTC),
        )
        session.add(run)
        await session.commit()
        return run.id


def _executor(database, supervisor, **overrides) -> RealRunExecutor:
    options = {
        "session_factory": database,
        "supervisor": supervisor,
        "hard_runtime_seconds": 600,
    }
    options.update(overrides)
    return RealRunExecutor(**options)


@pytest.mark.asyncio
async def test_stop_request_cancels_and_closes_the_run(database, monkeypatch) -> None:
    monkeypatch.setattr(experiment_module, "RUN_GUARD_POLL_SECONDS", 0.02)
    run_id = await _running_run(database)
    supervisor = _FakeSupervisor("hang")
    executor = _executor(database, supervisor, stop_requested=lambda: True)

    result = await asyncio.wait_for(executor._drive_run(supervisor, run_id), timeout=5)

    assert isinstance(result, ExecutionResult)
    assert result.status == "CANCELLED"
    assert result.error == "BENCHMARK_INTERRUPTED"
    assert result.evaluation_report is None
    assert supervisor.cancelled == [run_id], "the official cancel handoff must run first"

    async with database() as session:
        stored = await session.get(SolveRun, run_id)
        assert stored.status == "CANCELLED"
        assert stored.finished_at is not None


@pytest.mark.asyncio
async def test_hard_deadline_cancels_a_run_that_never_terminates(database, monkeypatch) -> None:
    monkeypatch.setattr(experiment_module, "RUN_GUARD_POLL_SECONDS", 0.02)
    run_id = await _running_run(database)
    supervisor = _FakeSupervisor("hang")
    executor = _executor(database, supervisor, hard_runtime_seconds=0)

    result = await asyncio.wait_for(executor._drive_run(supervisor, run_id), timeout=5)

    assert result.status == "CANCELLED"
    assert result.error == "RUN_HARD_TIMEOUT"
    assert supervisor.cancelled == [run_id]

    async with database() as session:
        assert (await session.get(SolveRun, run_id)).status == "CANCELLED"


@pytest.mark.asyncio
async def test_a_finished_run_returns_its_outcome_without_cancelling(database, monkeypatch) -> None:
    monkeypatch.setattr(experiment_module, "RUN_GUARD_POLL_SECONDS", 0.02)
    run_id = await _running_run(database)
    supervisor = _FakeSupervisor("finish")
    executor = _executor(database, supervisor)

    outcome = await asyncio.wait_for(executor._drive_run(supervisor, run_id), timeout=5)

    assert supervisor.cancelled == []
    assert not isinstance(outcome, ExecutionResult)
    assert outcome.status == "COMPLETED_UNSOLVED"


@pytest.mark.asyncio
async def test_a_run_that_closed_itself_keeps_its_result(database, monkeypatch) -> None:
    monkeypatch.setattr(experiment_module, "RUN_GUARD_POLL_SECONDS", 0.02)
    run_id = await _running_run(database)
    supervisor = _FinishingOnCancelSupervisor(database)
    executor = _executor(database, supervisor, hard_runtime_seconds=0)

    result = await asyncio.wait_for(executor._drive_run(supervisor, run_id), timeout=5)

    assert result.status == "COMPLETED_SOLVED"
    assert result.error == "RUN_HARD_TIMEOUT"
    async with database() as session:
        assert (await session.get(SolveRun, run_id)).status == "COMPLETED_SOLVED"


class _CountingExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, request) -> ExecutionResult:
        self.calls += 1
        return ExecutionResult(
            run_id=None,
            status="COMPLETED_UNSOLVED",
            evaluation_report=None,
        )


@pytest.mark.asyncio
async def test_a_stopped_matrix_does_not_start_the_next_task(tmp_path) -> None:
    import json

    from app.benchmark.experiment import ExperimentRunner

    case_dir = tmp_path / "cases"
    case_dir.mkdir()
    for case_id in ("stop-a", "stop-b"):
        (case_dir / f"{case_id}.json").write_text(
            json.dumps(
                {
                    "case_id": case_id,
                    "vulnerability_type": "SQLI",
                    "target": "http://target.test/",
                }
            ),
            encoding="utf-8",
        )

    executor = _CountingExecutor()
    runner = ExperimentRunner(
        case_dir=case_dir,
        output_dir=tmp_path / "out",
        executor=executor,
        arms="off",
        repeats=1,
        stop_requested=lambda: executor.calls >= 1,
    )

    summary = await runner.run()

    assert executor.calls == 1, "a stop request must not start another task"
    assert summary["stopped_early"] is True
    assert summary["completed_tasks"] == 1
    assert summary["planned_tasks"] == 2


def test_a_signal_sets_the_stop_flag_instead_of_raising() -> None:
    import signal

    from app.benchmark.__main__ import _install_stop_request

    previous = signal.getsignal(signal.SIGINT)
    try:
        stop = _install_stop_request()
        assert not stop.is_set()
        signal.raise_signal(signal.SIGINT)
        assert stop.is_set()
        # A second signal re-raises so an unresponsive process can still die.
        with pytest.raises(KeyboardInterrupt):
            signal.raise_signal(signal.SIGINT)
    finally:
        signal.signal(signal.SIGINT, previous)
