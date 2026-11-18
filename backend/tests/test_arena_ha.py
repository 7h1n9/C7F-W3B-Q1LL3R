"""Availability guarantees for the arena batch loop.

A batch occupies the platform's single environment slot for hours, with nobody
watching it.  Every failure mode here has the same shape: **the loop stops
working while still looking healthy from the outside.**  A stopped scheduler and
a scheduler waiting on a slow challenge render identically; an item stuck in
``RUNNING`` is invisible to the dispatcher because it only reads ``PENDING``; and
an environment leaked by a dead process fails every later solve with an error
that points at the challenge rather than at the leak.

These tests pin the behaviours that turn each of those into something visible or
self-correcting:

* the loop survives a failure to read its own backlog (it used to end the batch);
* one wedged item cannot hold the slot or the loop past the watchdog;
* the two repairs that exist -- requeue and destroy -- leave the in-flight work
  alone and never silently lose a record;

and they pin the *pure* rules (bulk-action eligibility, task-history eviction,
lease breadcrumb IO) that decide whether an operator click does the right thing.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from types import SimpleNamespace

import pytest

from app.api.v1 import arena as arena_api
from app.services import arena_lease as lease
from app.services import arena_queue as queue

# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


class _ScalarResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return list(self._rows)


class FakeSession:
    """The narrow slice of ``AsyncSession`` these code paths actually touch."""

    def __init__(self, rows: list) -> None:
        self.rows = rows
        self.commits = 0

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def scalars(self, _statement: object) -> _ScalarResult:
        return _ScalarResult(self.rows)

    async def commit(self) -> None:
        self.commits += 1


class FakeQueueRow:
    def __init__(self, item_id: str, status: str) -> None:
        self.id = item_id
        self.status = status
        self.last_error = None
        self.attempt_count = 1
        self.finished_at = object()
        self.priority = 100


class FakeArenaClient:
    """A delete that succeeds, fails, or answers ``NOT_FOUND`` like the platform.

    ``gone`` reproduces the real shape the arena returns for an environment that
    no longer exists: a ``ToolError`` carrying ``code="NOT_FOUND"``, which the
    reclaim path must read as "already destroyed" rather than as a failure.
    """

    def __init__(self, *, fail: bool = False, gone: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = fail
        self.gone = gone

    async def delete_environment(self, *, practice_ground_id: str, challenge_id: str) -> dict:
        self.calls.append((practice_ground_id, challenge_id))
        if self.gone:
            from app.services.arena_mcp import ToolError

            raise ToolError("NOT_FOUND", "The requested resource was not found or is not visible.")
        if self.fail:
            raise RuntimeError("arena unreachable")
        return {"deleted": True}


def _scheduler_without_recover(monkeypatch: pytest.MonkeyPatch) -> queue.ArenaQueueScheduler:
    """A scheduler whose startup sweep is a no-op, so tests need no database."""
    scheduler = queue.ArenaQueueScheduler()

    async def _recover(*_args: object, **_kwargs: object) -> int:
        return 0

    monkeypatch.setattr(scheduler, "recover", _recover)
    return scheduler


# --------------------------------------------------------------------------- #
# the loop must outlive its data
# --------------------------------------------------------------------------- #


async def test_loop_survives_a_backlog_read_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A read failure is a hiccup, not the end of the batch.

    This is the regression: the backlog read sat outside the per-item guard, so a
    transient database error raised straight out of ``_loop`` and completed the
    task.  ``running`` then read False, the UI said "调度未启动", and a hours-long
    batch was over -- with one log line as the only evidence.
    """
    monkeypatch.setattr(queue, "SCHEDULER_ERROR_BACKOFF_SECONDS", 0.01)
    monkeypatch.setattr(queue, "IDLE_SLEEP_SECONDS", 0.01)
    scheduler = _scheduler_without_recover(monkeypatch)

    calls = {"n": 0}

    async def flaky_pick() -> str | None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db gone")
        return None

    monkeypatch.setattr(scheduler, "_next_item_id", flaky_pick)
    await scheduler.start()
    for _ in range(200):
        await asyncio.sleep(0.01)
        if calls["n"] >= 3:
            break

    assert scheduler.running, "the loop ended on a transient read failure"
    assert calls["n"] >= 3, "the loop stopped retrying after the failure"

    health = scheduler.health()
    assert health["consecutive_failures"] == 0, "a successful read must clear the failure count"
    assert health["last_error"] is None

    await scheduler.stop()
    with contextlib.suppress(asyncio.CancelledError):
        await scheduler._task


async def test_loop_records_the_failure_it_survived(monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure has to be *readable*, not just survived.

    A loop that silently retries forever is indistinguishable from a loop that is
    working, so the status page would still show a healthy batch while nothing
    progresses.  Both the counter and the message must be visible in between.
    """
    monkeypatch.setattr(queue, "SCHEDULER_ERROR_BACKOFF_SECONDS", 30.0)
    scheduler = _scheduler_without_recover(monkeypatch)

    async def always_fails() -> str | None:
        raise RuntimeError("pool exhausted")

    monkeypatch.setattr(scheduler, "_next_item_id", always_fails)
    await scheduler.start()
    for _ in range(200):
        await asyncio.sleep(0.01)
        if scheduler.health()["consecutive_failures"]:
            break

    health = scheduler.health()
    assert health["consecutive_failures"] == 1
    assert "pool exhausted" in (health["last_error"] or "")
    assert health["running"] is True

    await scheduler.stop()
    with contextlib.suppress(asyncio.CancelledError):
        await scheduler._task


# --------------------------------------------------------------------------- #
# one wedged item must not hold the slot, or the loop
# --------------------------------------------------------------------------- #


async def test_watchdog_bounds_a_wedged_item(monkeypatch: pytest.MonkeyPatch) -> None:
    """``arena_solve`` bounds itself; this is the backstop for what it cannot.

    A hung HTTP call inside a solve is not covered by the solve's own budget, and
    an unbounded ``_process`` keeps the platform's only environment slot busy
    while stopping the whole batch.
    """
    monkeypatch.setattr(queue, "ARENA_ITEM_WATCHDOG_SECONDS", 0.05)
    monkeypatch.setattr(queue, "IDLE_SLEEP_SECONDS", 0.01)
    monkeypatch.setattr(queue, "SCHEDULER_ERROR_BACKOFF_SECONDS", 0.01)
    scheduler = _scheduler_without_recover(monkeypatch)

    picked = {"n": 0}

    async def one_item() -> str | None:
        picked["n"] += 1
        return "item-1" if picked["n"] == 1 else None

    async def wedged(_item_id: str) -> None:
        await asyncio.sleep(3600)

    handled: list[str] = []

    async def record(item_id: str) -> None:
        handled.append(item_id)

    monkeypatch.setattr(scheduler, "_next_item_id", one_item)
    monkeypatch.setattr(scheduler, "_process", wedged)
    monkeypatch.setattr(scheduler, "_handle_watchdog_timeout", record)

    await scheduler.start()
    for _ in range(300):
        await asyncio.sleep(0.01)
        if handled:
            break

    assert handled == ["item-1"], "the wedged item was never timed out"
    assert scheduler.running, "a watchdog fire must not end the loop"
    assert scheduler.health()["watchdog_timeouts"] == 1

    await scheduler.stop()
    with contextlib.suppress(asyncio.CancelledError):
        await scheduler._task


async def test_watchdog_timeout_releases_the_lease_and_requeues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The leak this closes: an environment held by an item that no longer runs."""

    released = {"n": 0}

    class FakeLeaseManager:
        async def release(self, *_args: object, **_kwargs: object) -> None:
            released["n"] += 1

    monkeypatch.setattr(queue, "arena_lease_manager", FakeLeaseManager())
    scheduler = queue.ArenaQueueScheduler()
    marked: list[tuple[str, str]] = []

    async def mark(item_id: str, message: str) -> None:
        marked.append((item_id, message))

    monkeypatch.setattr(scheduler, "_mark_failed", mark)
    await scheduler._handle_watchdog_timeout("item-9")

    assert released["n"] == 1, "a timed-out item must not leave its environment running"
    assert marked and marked[0][0] == "item-9"
    assert "watchdog" in marked[0][1]


# --------------------------------------------------------------------------- #
# repairs must spare the work in flight
# --------------------------------------------------------------------------- #


async def test_recover_leaves_the_in_flight_item_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Requeueing the item a live solve owns would race that solve.

    ``recover`` used to return *every* ``RUNNING`` row to ``PENDING``, which is
    correct at startup (nothing is in flight) and wrong while the scheduler runs.
    """
    live = FakeQueueRow("live", queue.RUNNING)
    dead = FakeQueueRow("dead", queue.RUNNING)
    session = FakeSession([live, dead])
    monkeypatch.setattr(queue, "SessionLocal", lambda: session)

    scheduler = queue.ArenaQueueScheduler()
    repaired = await scheduler.recover(exclude_item_id="live")

    assert repaired == 1
    assert live.status == queue.RUNNING, "the in-flight item was yanked out from under its solve"
    assert dead.status == queue.PENDING
    assert session.commits == 1


async def test_restarts_do_not_retire_a_challenge(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bug that made every restart cost the backlog a challenge.

    ``_process`` increments a counter when it *claims* an item, and the retry
    budget read that same counter.  A restart returns the item to ``PENDING``
    without it ever having run, so two restarts exhausted a budget meant for two
    genuine failures -- and with a handful of restarts during a session, every
    queued challenge reached the cap and would have been marked FAILED in turn.

    The budget now reads ``outcome_count`` (verdicts reached), and a claim must
    not touch it.
    """
    scheduler = queue.ArenaQueueScheduler()

    # Two claims (e.g. one solve attempt, then a restart and a re-claim) with no
    # verdict recorded: still eligible.
    claimed = SimpleNamespace(id="a", status=queue.PENDING, outcome_count=0, attempt_count=2)
    session = FakeSession([claimed])
    monkeypatch.setattr(queue, "SessionLocal", lambda: session)
    monkeypatch.setattr(scheduler, "_candidates", _candidates_of([claimed]))
    assert await scheduler._next_item_id() == "a"

    # Two verdicts: now genuinely exhausted.
    spent = SimpleNamespace(id="b", status=queue.PENDING, outcome_count=2, attempt_count=2)
    monkeypatch.setattr(scheduler, "_candidates", _candidates_of([spent]))
    assert await scheduler._next_item_id() is None


def _candidates_of(rows: list) -> object:
    async def _candidates(_session: object) -> list:
        return list(rows)

    return _candidates


async def test_start_sweeps_stranded_items_before_its_first_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restart must not silently skip the challenge it was interrupted on.

    ``_next_item_id`` only reads ``PENDING``, so an item left ``RUNNING`` by a
    previous process is invisible to a newly started scheduler.
    """
    monkeypatch.setattr(queue, "IDLE_SLEEP_SECONDS", 0.01)
    scheduler = queue.ArenaQueueScheduler()
    swept = {"n": 0}

    async def sweep(*_args: object, **_kwargs: object) -> int:
        swept["n"] += 1
        return 2

    monkeypatch.setattr(scheduler, "recover", sweep)

    async def no_item() -> str | None:
        return None

    monkeypatch.setattr(scheduler, "_next_item_id", no_item)
    await scheduler.start()
    assert swept["n"] == 1, "start() began a batch without sweeping stranded items"

    await scheduler.stop()
    with contextlib.suppress(asyncio.CancelledError):
        await scheduler._task


# --------------------------------------------------------------------------- #
# the lease breadcrumb: the only way to name a leaked environment
# --------------------------------------------------------------------------- #


def _use_lease_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> str:
    path = str(tmp_path / "arena-lease.json")
    monkeypatch.setattr(
        lease, "get_settings", lambda: SimpleNamespace(arena_lease_state_file=path)
    )
    return path


async def test_persisted_lease_is_destroyed_and_cleared(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The round trip that turns a leaked slot into a one-line repair."""
    _use_lease_file(monkeypatch, tmp_path)
    lease._persist_lease(
        lease.Lease(
            connection_id="conn-1",
            practice_ground_id="ground-1",
            challenge_id="chal-1",
            environment_id="env-1",
        )
    )
    recorded = lease._read_state()
    assert recorded and recorded["challenge_id"] == "chal-1"

    client = FakeArenaClient()
    result = await lease.reclaim_persisted_lease(client, connection_id="conn-1")

    assert result["reclaimed"] is True
    assert client.calls == [("ground-1", "chal-1")]
    assert lease._read_state() is None, "a destroyed environment must not leave its record behind"


async def test_reclaim_refuses_another_connections_lease(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Destroying another connection's environment would break a solve we do not own."""
    _use_lease_file(monkeypatch, tmp_path)
    lease._write_state({"connection_id": "conn-2", "practice_ground_id": "g", "challenge_id": "c"})

    client = FakeArenaClient()
    result = await lease.reclaim_persisted_lease(client, connection_id="conn-1")

    assert result["reclaimed"] is False
    assert result["reason"] == "lease_belongs_to_another_connection"
    assert client.calls == []
    assert lease._read_state() is not None


async def test_failed_destroy_keeps_the_record(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Keeping the breadcrumb on failure is what lets the next attempt retry.

    Clearing it here would lose the only record of an environment that is still
    running -- exactly the state this file exists to get out of.
    """
    _use_lease_file(monkeypatch, tmp_path)
    lease._write_state({"connection_id": "conn-1", "practice_ground_id": "g", "challenge_id": "c"})

    result = await lease.reclaim_persisted_lease(FakeArenaClient(fail=True), connection_id="conn-1")

    assert result["reclaimed"] is False
    assert result["reason"] == "delete_failed"
    assert lease._read_state() is not None


async def test_not_found_destroy_clears_the_record(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """An environment the platform has already forgotten is reclaimed, not failed.

    This is the ghost-breadcrumb case: a crash's record survives on disk while
    the platform expires the environment.  The delete then answers ``NOT_FOUND``.
    Treating that as a failure left the record in place forever, so every later
    start kept a stale "the slot is taken" breadcrumb -- the exact wedge the file
    exists to clear.  ``NOT_FOUND`` must therefore take the successful path.
    """
    _use_lease_file(monkeypatch, tmp_path)
    lease._write_state({"connection_id": "conn-1", "practice_ground_id": "g", "challenge_id": "c"})

    client = FakeArenaClient(gone=True)
    result = await lease.reclaim_persisted_lease(client, connection_id="conn-1")

    assert result["reclaimed"] is True
    assert result["reason"] == "already_gone"
    assert client.calls == [("g", "c")]
    assert lease._read_state() is None, "a gone environment must not leave its record behind"


async def test_release_clears_the_record_when_the_environment_is_already_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """``release`` has the same ghost case as ``reclaim`` and must also clear it.

    A Run can finish *after* the platform has expired its environment; the
    breadcrumb would otherwise outlive the solve that owned it.
    """
    _use_lease_file(monkeypatch, tmp_path)

    manager = lease.ArenaLeaseManager()
    async with manager as held:
        held._current = lease.Lease(
            connection_id="conn-1", practice_ground_id="g", challenge_id="c", environment_id="env-1"
        )
        lease._persist_lease(held._current)
        assert lease._read_state() is not None
        await held.release(FakeArenaClient(gone=True))

    assert lease._read_state() is None


async def test_reclaim_with_no_record_is_a_clean_no_op(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The ordinary startup case: nothing to reclaim, and no outbound call."""
    _use_lease_file(monkeypatch, tmp_path)
    client = FakeArenaClient()

    result = await lease.reclaim_persisted_lease(client, connection_id="conn-1")

    assert result == {"reclaimed": False, "reason": "no_lease_record", "lease": None}
    assert client.calls == []


async def test_explicit_challenge_overrides_a_missing_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """An operator who knows the challenge must not be blocked by a lost file."""
    _use_lease_file(monkeypatch, tmp_path)
    client = FakeArenaClient()

    result = await lease.reclaim_persisted_lease(
        client, connection_id="conn-1", practice_ground_id="g", challenge_id="c"
    )

    assert result["reclaimed"] is True
    assert client.calls == [("g", "c")]


# --------------------------------------------------------------------------- #
# pure rules behind the interactive surface
# --------------------------------------------------------------------------- #


def test_health_exposes_liveness(monkeypatch: pytest.MonkeyPatch) -> None:
    """The status page reads these; a missing key would be a blank panel."""
    health = queue.ArenaQueueScheduler().health()

    assert health["running"] is False
    assert health["last_tick_age_seconds"] is None
    assert health["current_item_age_seconds"] is None
    assert health["watchdog_seconds"] == queue.ARENA_ITEM_WATCHDOG_SECONDS
    for key in (
        "current_item_id",
        "uptime_seconds",
        "processed_items",
        "watchdog_timeouts",
        "consecutive_failures",
        "last_error",
    ):
        assert key in health


def test_health_separates_tick_age_from_item_age() -> None:
    """The two answer different questions and must not be conflated.

    Reporting only the tick age made a healthy twenty-minute solve render as
    "疑似卡死": the loop is inside ``_process`` during an item and simply is not
    ticking.  A status page that cries wolf on the normal case trains the
    operator to ignore it.
    """
    scheduler = queue.ArenaQueueScheduler()
    scheduler._last_tick_at = time.monotonic() - 900.0
    scheduler._current_started_at = time.monotonic() - 870.0

    health = scheduler.health()

    assert health["last_tick_age_seconds"] >= 890
    assert health["current_item_age_seconds"] >= 860
    # Both are "old", and the presence of the second is what tells the reader this
    # is a long item rather than a wedged loop.
    assert health["current_item_age_seconds"] is not None


def test_watchdog_outlasts_the_slowest_legitimate_solve() -> None:
    """A ceiling below the worst legitimate spend is worse than none.

    It would destroy the environment underneath a slow-but-working solve and turn
    a success into a timeout, so the figure is derived from the phases it must
    clear rather than picked.
    """
    from app.services import arena_lease, arena_solve

    assert queue.ARENA_ITEM_WATCHDOG_SECONDS > (
        arena_lease.READY_TIMEOUT_SECONDS + arena_solve.RUN_TIMEOUT_SECONDS
    )


def _row(**overrides: object) -> SimpleNamespace:
    base = {
        "id": "row-1",
        "status": "PENDING",
        "priority": 100,
        "attempt_count": 2,
        "outcome_count": 2,
        "last_error": "boom",
        "finished_at": object(),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_bulk_retry_resets_both_counters() -> None:
    """Leaving either counter at the cap would skip the row just asked to run.

    ``outcome_count`` is the one the budget reads, so resetting only the claim
    count would leave the scheduler refusing to pick the row.
    """
    item = _row(status="FAILED")

    reason, should_delete = arena_api._apply_queue_bulk_action(item, "retry")

    assert (reason, should_delete) == (None, False)
    assert item.status == "PENDING"
    assert item.attempt_count == 0
    assert item.outcome_count == 0
    assert item.last_error is None
    assert item.finished_at is None


def test_bulk_never_touches_a_running_row() -> None:
    """Retrying races the live solve; deleting loses a result already paid for."""
    running = _row(status="RUNNING")

    assert arena_api._apply_queue_bulk_action(running, "retry") == ("running", False)
    assert arena_api._apply_queue_bulk_action(running, "delete") == ("running", False)
    assert running.status == "RUNNING"


def test_bulk_cancel_and_delete_respect_finished_rows() -> None:
    assert arena_api._apply_queue_bulk_action(_row(status="SOLVED"), "cancel") == (
        "already_finished",
        False,
    )
    assert arena_api._apply_queue_bulk_action(_row(status="FAILED"), "delete") == (None, True)

    pending = _row(status="PENDING")
    assert arena_api._apply_queue_bulk_action(pending, "cancel") == (None, False)
    assert pending.status == "CANCELLED"


def test_bulk_top_honours_the_rank_the_caller_computes() -> None:
    """A fixed step would tie a multi-row selection and lose its order.

    Lower runs first, so the route hands out ascending ranks in the order it
    processes the selection; the helper's contract is simply to use the value.
    """
    first = _row(status="PENDING")
    second = _row(status="PENDING")

    assert arena_api._apply_queue_bulk_action(first, "top", top_priority=-2) == (None, False)
    assert arena_api._apply_queue_bulk_action(second, "top", top_priority=-1) == (None, False)

    assert (first.priority, second.priority) == (-2, -1)
    assert first.priority < second.priority, "the first row of the selection must run first"

    finished = _row(status="FAILED")
    assert arena_api._apply_queue_bulk_action(finished, "top", top_priority=-3) == (
        "already_finished",
        False,
    )
    assert finished.priority == 100


def test_solve_task_history_is_bounded_but_never_evicts_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The task dict lives for the process lifetime and reports must not leak."""
    monkeypatch.setattr(arena_api, "_solve_tasks", {})
    monkeypatch.setattr(arena_api, "ARENA_SOLVE_TASK_HISTORY", 3)

    for index in range(6):
        arena_api._remember_solve_task(f"t{index}", {"state": "finished"})
    assert len(arena_api._solve_tasks) == 3

    arena_api._solve_tasks["live"] = {"state": "running"}
    arena_api._remember_solve_task("fresh", {"state": "finished"})

    assert "live" in arena_api._solve_tasks, "a running task's handle was evicted"
    assert "fresh" in arena_api._solve_tasks
    assert len(arena_api._solve_tasks) <= 3
