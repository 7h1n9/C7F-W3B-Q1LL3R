import asyncio
import importlib
from types import SimpleNamespace

import pytest

from app.services.run_supervisor import RunSupervisor

run_supervisor_module = importlib.import_module("app.services.run_supervisor")


@pytest.mark.asyncio
async def test_cancel_muteki_cancels_and_awaits_active_task(monkeypatch) -> None:
    supervisor = RunSupervisor()
    stopped = asyncio.Event()

    async def runtime() -> None:
        try:
            await asyncio.sleep(60)
        finally:
            stopped.set()

    task = asyncio.create_task(runtime())
    supervisor._active_run_tasks["run-1"] = task
    await asyncio.sleep(0)
    monkeypatch.setattr(run_supervisor_module, "asyncio", asyncio)

    class _ContainerExec:
        @staticmethod
        def teardown_container(run_id: str, *, remove: bool = True) -> None:
            assert run_id == "run-1"
            assert remove is True

    monkeypatch.setitem(__import__("sys").modules, "muteki.solver.container_exec", _ContainerExec)
    assert await supervisor.cancel_muteki("run-1", grace_seconds=1) is True
    assert stopped.is_set()
    assert task.done()


@pytest.mark.asyncio
async def test_cancel_muteki_reaps_orphan_container_without_restarting_worker(monkeypatch) -> None:
    supervisor = RunSupervisor()
    calls: list[tuple[str, bool]] = []

    class _ContainerExec:
        @staticmethod
        def teardown_container(run_id: str, *, remove: bool = True) -> None:
            calls.append((run_id, remove))

    monkeypatch.setitem(__import__("sys").modules, "muteki.solver.container_exec", _ContainerExec)
    assert await supervisor.cancel_muteki("orphan-run") is False
    assert calls == [("orphan-run", True)]
    assert supervisor._active_run_tasks == {}


@pytest.mark.asyncio
async def test_startup_reaps_only_terminal_muteki_runs(monkeypatch) -> None:
    supervisor = RunSupervisor()
    calls: list[str] = []

    class _ContainerExec:
        @staticmethod
        def teardown_container(run_id: str, *, remove: bool = True) -> None:
            assert remove is True
            calls.append(run_id)

    monkeypatch.setitem(__import__("sys").modules, "muteki.solver.container_exec", _ContainerExec)
    runs = [
        SimpleNamespace(id="terminal-muteki", solver_mode="muteki", status="CANCELLED"),
        SimpleNamespace(id="live-muteki", solver_mode="muteki", status="RUNNING"),
        SimpleNamespace(id="terminal-legacy", solver_mode="solver_v2", status="CANCELLED"),
    ]

    assert await supervisor.reap_terminal_muteki_containers(runs) == 1
    assert calls == ["terminal-muteki"]


@pytest.mark.asyncio
async def test_terminal_muteki_run_is_not_restarted_by_supervisor(monkeypatch) -> None:
    supervisor = RunSupervisor()
    run = SimpleNamespace(
        id="cancelled-run",
        solver_mode="muteki",
        status="CANCELLED",
        current_phase="REPORTING",
        last_error_code="RUN_CANCELLED",
        recovery_checkpoint_json={},
    )

    class _Session:
        async def get(self, _model, _run_id):
            return run

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("terminal Muteki Run must not re-enter runtime")

    monkeypatch.setattr(supervisor, "_run_muteki", fail_if_called)
    outcome = await supervisor.continue_until_terminal(_Session(), "cancelled-run")
    assert outcome.status == "CANCELLED"
