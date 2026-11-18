"""A Run whose Workers never executed anything must not spend the whole budget.

Measured 2026-11-02, two challenges in a row (``d885351d`` GhostScript 26 min,
``8d1c...`` Joomla 13 min): the container had zero new files, zero
``muteki.worker_command`` events, and the host held nine dead SSE connections
from ``codex.exe`` to the codex proxy.  The CLI could not reach its relay, so it
reconnected in a loop while the Run held the platform's only environment slot
until its own budget expired.

``RUN_TIMEOUT_SECONDS`` is the bound for a Run that is *working*.  These tests
pin the second bound: no command within ``RUN_STALL_GRACE_SECONDS`` means the
Run is wedged, and it is cancelled instead of burning the rest of the budget.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services import arena_solve


class _FakeSession:
    """Enough of ``AsyncSession`` for ``wait_for_run``: two queries and commits."""

    def __init__(self, executed: int | None) -> None:
        self.executed = executed
        self.commits = 0
        self.count_queries = 0

    async def scalar(self, statement):  # noqa: ANN001 - SQLAlchemy Select
        text = str(statement)
        if "run_events" in text:
            self.count_queries += 1
            return self.executed
        return "RUNNING"

    async def commit(self) -> None:
        self.commits += 1

    async def get(self, model, key, **kwargs):  # noqa: ANN001, ANN003
        return None


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse the poll interval so the test spends no wall-clock time."""

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(arena_solve.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(arena_solve, "RUN_POLL_INTERVAL_SECONDS", 1.0)
    monkeypatch.setattr(arena_solve, "RUN_STALL_GRACE_SECONDS", 2.0)


@pytest.mark.asyncio
async def test_a_run_with_no_worker_command_is_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession(executed=0)
    cancelled: list[tuple[str, str]] = []

    async def _cancel(_session, run_id, reason="") -> None:
        cancelled.append((run_id, reason))

    monkeypatch.setattr(arena_solve, "cancel_run", _cancel)

    status = await arena_solve.wait_for_run(session, "run-1", timeout=10)

    assert status == "STALLED_NO_WORKER_COMMAND"
    assert len(cancelled) == 1
    assert cancelled[0][0] == "run-1"
    # The reason has to name the bound, or the operator cannot tell this apart
    # from a user pressing cancel.
    assert "No Worker command within" in cancelled[0][1]


@pytest.mark.asyncio
async def test_the_progress_query_is_asked_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Run that is working must not be re-counted every poll for 25 minutes."""

    session = _FakeSession(executed=4)
    monkeypatch.setattr(arena_solve, "cancel_run", _cancel_that_must_not_run)

    status = await arena_solve.wait_for_run(session, "run-2", timeout=8)

    assert status == "TIMEOUT_WAITING"
    assert session.count_queries == 1


@pytest.mark.asyncio
async def test_a_failed_cancel_keeps_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reporting "stalled" for a Run that is still live would release its env."""

    session = _FakeSession(executed=0)

    async def _cancel(_session, run_id, reason="") -> None:
        raise RuntimeError("database is gone")

    monkeypatch.setattr(arena_solve, "cancel_run", _cancel)

    status = await arena_solve.wait_for_run(session, "run-3", timeout=6)

    assert status == "TIMEOUT_WAITING"


@pytest.mark.asyncio
async def test_a_terminal_run_is_reported_before_the_stall_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No cancelling a Run that already closed itself."""

    class _Done(_FakeSession):
        async def scalar(self, statement):  # noqa: ANN001
            if "run_events" in str(statement):
                self.count_queries += 1
                return 0
            return "COMPLETED_UNSOLVED"

    session = _Done(executed=0)
    monkeypatch.setattr(arena_solve, "cancel_run", _cancel_that_must_not_run)

    assert await arena_solve.wait_for_run(session, "run-4", timeout=10) == "COMPLETED_UNSOLVED"
    assert session.count_queries == 0


@pytest.mark.asyncio
async def test_the_wait_still_returns_after_the_timeout() -> None:
    """The stall check must not become an unbounded loop."""

    session = _FakeSession(executed=1)
    assert await arena_solve.wait_for_run(session, "run-5", timeout=3) == "TIMEOUT_WAITING"


async def _cancel_that_must_not_run(*_args, **_kwargs) -> None:
    raise AssertionError("cancel_run must not be called here")


def test_asyncio_is_the_module_used_for_sleep() -> None:
    """Guard the fixture: patching a re-exported asyncio would silently no-op."""

    assert arena_solve.asyncio is asyncio
