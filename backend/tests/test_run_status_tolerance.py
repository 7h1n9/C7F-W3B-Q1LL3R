"""An unrecognised persisted Run status must not stop the API from starting.

A ``solve_runs.status`` of ``FAILED`` -- a value the enum has never had -- was
found in the database.  Nothing in the current tree writes it, so it came from
outside the enum: a hand edit, or a revision that predates the current spelling.
The origin matters less than the blast radius:

* ``RunStatus(run.status)`` raises on it, and that read sat in
  ``reconcile_muteki_native_terminals``, which is called at startup *before*
  lease reclamation, phase projection and container reaping.
* One exception there skipped the whole sweep for every Run in the database, and
  the only trace was a ``logger.exception`` -- the API came up healthy while
  every stale row stayed stale.

So the tests below pin two properties: an unknown status parses to "not terminal"
rather than raising, and a per-Run failure cannot abort the sweep.
"""

from __future__ import annotations

import pytest

from app.orchestration.state_machine import TERMINAL, RunStatus, parse_run_status
from app.services import run_supervisor as run_supervisor_module
from app.services.run_supervisor import run_supervisor


@pytest.mark.parametrize("status", list(RunStatus))
def test_known_statuses_round_trip(status: RunStatus) -> None:
    assert parse_run_status(status.value) == status


def test_enum_instance_passes_through() -> None:
    assert parse_run_status(RunStatus.RUNNING) is RunStatus.RUNNING


@pytest.mark.parametrize("unknown", ["FAILED", "failed", "", "None", "COMPLETED", 7])
def test_unknown_status_parses_to_none(unknown: object) -> None:
    assert parse_run_status(unknown) is None


def test_unknown_status_is_never_treated_as_terminal() -> None:
    """The safe reading: unknown means "keep reconciling", not "call it done".

    The guard the callers use is ``if status is not None and status in TERMINAL``,
    so an unknown status falls through to reconciliation instead of
    short-circuiting it -- which is what keeps a bad row eligible for repair.
    """
    parsed = parse_run_status("FAILED")
    assert parsed is None
    assert parsed not in TERMINAL
    assert RunStatus.FAILED_ENGINE in TERMINAL and RunStatus.COMPLETED_SOLVED in TERMINAL


class _StubSession:
    """Just enough of AsyncSession for a reconciliation read."""

    def __init__(self, runs: dict) -> None:
        self._runs = runs
        self.scalar_calls = 0

    async def get(self, model, key, **kwargs):  # noqa: ANN001
        return self._runs.get(key)

    async def scalar(self, *args, **kwargs):  # noqa: ANN002, ANN003
        self.scalar_calls += 1
        return None


class _StubRun:
    def __init__(self, run_id: str, status: str) -> None:
        self.id = run_id
        self.status = status
        self.solver_mode = "muteki"


async def test_unknown_status_does_not_raise_in_terminal_reconciliation() -> None:
    """The exact row that was in the database: muteki + status 'FAILED'."""
    session = _StubSession({})
    run = _StubRun("run-bad-status", "FAILED")

    # No lease and no terminal event, so the answer is "nothing to project" --
    # the point is that it is an answer, not a ValueError.
    assert await run_supervisor.reconcile_muteki_native_terminal(session, run) is None


async def test_known_terminal_status_short_circuits_before_any_query() -> None:
    session = _StubSession({})
    run = _StubRun("run-done", RunStatus.COMPLETED_SOLVED.value)

    assert await run_supervisor.reconcile_muteki_native_terminal(session, run) is None
    assert session.scalar_calls == 0


async def test_one_bad_run_cannot_abort_the_sweep(monkeypatch) -> None:  # noqa: ANN001
    runs = {
        "run-good-1": _StubRun("run-good-1", RunStatus.RUNNING.value),
        "run-bad": _StubRun("run-bad", "FAILED"),
        "run-good-2": _StubRun("run-good-2", RunStatus.RUNNING.value),
    }
    session = _StubSession(runs)
    attempted: list[str] = []

    async def fake_reconcile(_session, run):  # noqa: ANN001
        attempted.append(run.id)
        if run.id == "run-bad":
            raise ValueError("'FAILED' is not a valid RunStatus")
        return object()

    monkeypatch.setattr(run_supervisor, "reconcile_muteki_native_terminal", fake_reconcile)

    reconciled = await run_supervisor.reconcile_muteki_native_terminals(
        session, list(runs.values())
    )

    # The bad row did not stop the runs after it, and the count reports only
    # the ones that actually reconciled.
    assert attempted == ["run-good-1", "run-bad", "run-good-2"]
    assert reconciled == 2


def test_module_instance_is_the_one_under_test() -> None:
    assert run_supervisor is run_supervisor_module.run_supervisor


# --------------------------------------------------------------------------- #
# The same class of bug in the *attempt* sweep.
#
# ``run_attempt_service.reconcile_startup`` reads ``RunStatus(run.status)`` while
# walking every lease in the database, and that read is evaluated before the
# lease's own expiry flags.  So an unreadable status on any one row used to abort
# the whole attempt/lease sweep with a ValueError -- the leases stayed, the
# attempts stayed RUNNING, and startup carried on as if it had worked.
# --------------------------------------------------------------------------- #


class _ScalarRows:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return list(self._rows)


class _LeaseStubSession:
    """Enough AsyncSession for ``reconcile_startup``'s two query shapes.

    Call order is fixed by the implementation: leases (walk), leases again (to
    compute the still-active attempt ids), then the RUNNING attempts.
    """

    def __init__(self, leases: list, runs: dict, attempts: dict) -> None:
        self._leases = leases
        self._runs = runs
        self._attempts = attempts
        self._scalars_calls = 0
        self.deleted_leases: list = []
        self.commits = 0

    async def scalars(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        self._scalars_calls += 1
        if self._scalars_calls <= 2:
            return _ScalarRows(self._leases)
        return _ScalarRows([a for a in self._attempts.values() if a.status == "RUNNING"])

    async def get(self, model, key, **_kwargs):  # noqa: ANN001
        name = getattr(model, "__name__", "")
        if name == "SolveRun":
            return self._runs.get(key)
        if name == "RunAttempt":
            return self._attempts.get(key)
        return None

    async def execute(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        return None

    async def delete(self, obj) -> None:  # noqa: ANN001
        self.deleted_leases.append(obj)

    async def commit(self) -> None:
        self.commits += 1


class _Lease:
    def __init__(self, run_id: str, attempt_id: str, *, expires_at, owner: str) -> None:
        self.id = f"lease-{run_id}"
        self.run_id = run_id
        self.attempt_id = attempt_id
        self.expires_at = expires_at
        self.owner_instance_id = owner


async def test_expired_lease_with_unreadable_run_status_does_not_raise() -> None:
    """The read that used to be ``RunStatus(run.status)`` must tolerate garbage.

    Evaluated before the expiry flags, it raised on the unknown status and the
    whole sweep died -- so this pins that an unknown status now simply reads as
    "not terminal" and the lease is reclaimed anyway.
    """
    from datetime import UTC, datetime, timedelta

    from app.services.run_attempts import run_attempt_service

    expired = datetime.now(UTC) - timedelta(hours=1)
    attempt = _StubRun("run-bad-attempt", RunStatus.RUNNING.value)
    attempt.status = "RUNNING"
    lease = _Lease("run-bad", "att-1", expires_at=expired, owner="another-process")
    session = _LeaseStubSession(
        [lease],
        {"run-bad": _StubRun("run-bad", "FAILED")},
        {"att-1": attempt},
    )

    result = await run_attempt_service.reconcile_startup(session)

    assert result["leases_deleted"] == 1
    assert result["attempts_aborted"] == 1
    assert attempt.status == "ABORTED"
    assert attempt.error_code == "PROCESS_INTERRUPTED"
    assert session.deleted_leases == [lease]


async def test_terminal_run_status_still_closes_its_running_attempt() -> None:
    """The known-status path is unchanged: close the attempt with the Run's own terminal status."""
    from app.services.run_attempts import run_attempt_service

    attempt = _StubRun("run-done", RunStatus.RUNNING.value)
    attempt.status = "RUNNING"
    attempt.run_id = "run-done"
    attempt.error_code = None
    attempt.finished_at = None
    run = _StubRun("run-done", RunStatus.COMPLETED_SOLVED.value)
    run.finished_at = None
    run.last_error_code = None
    session = _LeaseStubSession([], {"run-done": run}, {"att-1": attempt})

    result = await run_attempt_service.reconcile_startup(session)

    assert result["attempts_closed"] == 1
    assert attempt.status == RunStatus.COMPLETED_SOLVED.value

