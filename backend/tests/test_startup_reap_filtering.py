"""Startup reaping must not cost two docker processes per persisted Run.

``teardown_container`` issues ``docker rm -f`` plus a legacy per-worker sweep, so
reaping unconditionally makes service startup proportional to the entire Run
history.  These tests pin the convergence: only Runs that still have a container
are handed to teardown, and any inability to answer that question degrades to the
old, unconditional behaviour rather than to silent skipping.
"""

import asyncio
import sys
from types import SimpleNamespace

import pytest

from app.services.run_supervisor import RunSupervisor, _runs_owning_a_container

MODULE = "muteki.solver.container_exec"


def _fake_container_exec(*, names=(), calls=None, listing_fails=False, with_helpers=True):
    """Stand in for ``muteki.solver.container_exec`` in ``sys.modules``."""

    class _Listing:
        stdout = "\n".join(names)

    if not with_helpers:
        # Mirrors the stub used by the pre-existing cancellation tests: only the
        # public teardown entry point exists.
        class _ContainerExec:
            @staticmethod
            def teardown_container(run_id, *, remove=True):
                assert remove is True
                if calls is not None:
                    calls.append(run_id)

        return _ContainerExec

    class _ContainerExec:
        @staticmethod
        def _docker(*args, timeout=30.0):
            if listing_fails:
                raise RuntimeError("docker unavailable")
            return _Listing()

        @staticmethod
        def _run_container_name(run_id):
            return f"muteki-run-{run_id}"[:120]

        @staticmethod
        def _safe(run_id):
            return run_id

        @staticmethod
        def teardown_container(run_id, *, remove=True):
            assert remove is True
            if calls is not None:
                calls.append(run_id)

    return _ContainerExec


def _run(run_id, status="CANCELLED", solver_mode="muteki"):
    return SimpleNamespace(id=run_id, solver_mode=solver_mode, status=status)


# --------------------------------------------------------------------------- filter


def test_only_runs_whose_container_still_exists_are_selected(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, MODULE, _fake_container_exec(names=["muteki-run-keep"]))

    assert _runs_owning_a_container(["keep", "long-gone"]) == ["keep"]


def test_an_empty_docker_listing_selects_nothing(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, MODULE, _fake_container_exec(names=[]))

    assert _runs_owning_a_container(["gone-1", "gone-2"]) == []


def test_legacy_per_worker_leftovers_still_count_as_owned(monkeypatch) -> None:
    monkeypatch.setitem(
        sys.modules, MODULE, _fake_container_exec(names=["muteki-w-gone-1-9f3a2b"])
    )

    assert _runs_owning_a_container(["gone-1", "gone-2"]) == ["gone-1"]


def test_an_unavailable_listing_is_reported_as_unknown(monkeypatch) -> None:
    monkeypatch.setitem(
        sys.modules, MODULE, _fake_container_exec(names=["muteki-run-x"], listing_fails=True)
    )

    assert _runs_owning_a_container(["any-run"]) is None


def test_a_module_without_listing_helpers_is_reported_as_unknown(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, MODULE, _fake_container_exec(with_helpers=False))

    assert _runs_owning_a_container(["any-run"]) is None


# --------------------------------------------------------------------------- reaping


@pytest.mark.asyncio
async def test_startup_reaps_only_owned_containers(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setitem(
        sys.modules, MODULE, _fake_container_exec(names=["muteki-run-keep"], calls=calls)
    )
    supervisor = RunSupervisor()

    reaped = await supervisor.reap_terminal_muteki_containers([_run("keep"), _run("gone")])

    assert reaped == 1
    assert calls == ["keep"]


@pytest.mark.asyncio
async def test_startup_skips_docker_entirely_when_nothing_is_left_over(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setitem(sys.modules, MODULE, _fake_container_exec(names=[], calls=calls))
    supervisor = RunSupervisor()

    reaped = await supervisor.reap_terminal_muteki_containers(
        [_run(f"run-{index}") for index in range(50)]
    )

    assert reaped == 0
    assert calls == []


@pytest.mark.asyncio
async def test_startup_still_reaps_everything_when_the_listing_fails(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setitem(
        sys.modules,
        MODULE,
        _fake_container_exec(names=["muteki-run-x"], calls=calls, listing_fails=True),
    )
    supervisor = RunSupervisor()

    reaped = await supervisor.reap_terminal_muteki_containers([_run("a"), _run("b")])

    assert reaped == 2
    assert calls == ["a", "b"]


@pytest.mark.asyncio
async def test_startup_still_reaps_everything_without_listing_helpers(monkeypatch) -> None:
    """The pre-existing contract: a vendor module we cannot introspect is reaped in full."""

    calls: list[str] = []
    monkeypatch.setitem(sys.modules, MODULE, _fake_container_exec(with_helpers=False, calls=calls))
    supervisor = RunSupervisor()

    reaped = await supervisor.reap_terminal_muteki_containers([_run("a"), _run("b")])

    assert reaped == 2
    assert calls == ["a", "b"]


@pytest.mark.asyncio
async def test_non_muteki_and_active_runs_are_never_reaped(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setitem(
        sys.modules,
        MODULE,
        _fake_container_exec(names=["muteki-run-keep", "muteki-run-active"], calls=calls),
    )
    supervisor = RunSupervisor()

    task = asyncio.create_task(asyncio.sleep(60))
    supervisor._active_run_tasks["active"] = task
    try:
        reaped = await supervisor.reap_terminal_muteki_containers(
            [
                _run("legacy", solver_mode="solver_v2"),
                _run("active", status="RUNNING"),
                _run("keep"),
            ]
        )
    finally:
        task.cancel()

    assert reaped == 1
    assert calls == ["keep"]
