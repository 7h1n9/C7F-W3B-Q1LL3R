"""A solved Run's flag must reach the ledger even when its own session is dead.

Run ``bc80a2fd`` (local phpMyAdmin, CVE-2018-12613) reached
``COMPLETED_SOLVED`` in 79 seconds holding the byte-exact flag, and
``flag_candidates`` was **empty**.  The backend log had the reason:

    PendingRollbackError: This Session's transaction has been rolled back ...
    unable to record runtime flag for run bc80a2fd-...

``_run_muteki`` shares one session with ``MutekiRuntime``.  Worker callbacks run
through ``asyncio.to_thread`` and touch that session from outside the loop, so
by the time a flag is reported the session can already be unusable.  The old
code logged a warning and moved on -- and every consumer of the ledger (the UI,
the submit path, the benchmark evaluator) then treated a real solve as
INCOMPLETE.

These tests pin the fix: the ledger write owns a fresh session, so it cannot be
lost to the solve transaction's fate, and a failure is loud instead of silent.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sqlalchemy.exc import PendingRollbackError

from app.services import run_supervisor as module
from app.services.run_supervisor import RunSupervisor


class _PoisonedSession:
    """Any use of the Run's own session raises, exactly like the real failure."""

    def _explode(self, *_args, **_kwargs):
        raise PendingRollbackError("transaction rolled back")

    get = _explode
    add = _explode
    commit = _explode
    flush = _explode
    scalar = _explode
    execute = _explode


class _LedgerSession:
    def __init__(self, rows: dict) -> None:
        self._rows = rows
        self.commits = 0

    async def get(self, model, key):  # noqa: ANN001 - duck-typed on purpose
        return self._rows.get((model.__name__, key))

    async def commit(self) -> None:
        self.commits += 1


class _SessionFactory:
    """``SessionLocal()`` replacement that hands out one known session."""

    def __init__(self, session: _LedgerSession) -> None:
        self._session = session
        self.opened = 0

    def __call__(self) -> "_SessionFactory":
        return self

    async def __aenter__(self) -> _LedgerSession:
        self.opened += 1
        return self._session

    async def __aexit__(self, *_exc) -> bool:
        return False


def test_flag_reaches_the_ledger_when_the_run_session_is_poisoned(monkeypatch) -> None:
    recorded: list[tuple[str, str]] = []

    async def _record(session, run, challenge, candidate, **_kwargs):  # noqa: ANN001
        recorded.append((run.id, candidate))
        assert isinstance(session, _LedgerSession)
        assert challenge.flag_pattern == r"flag\{[0-9a-f]{32}\}"
        return SimpleNamespace(id="cand-1")

    ledger = _LedgerSession(
        {
            ("SolveRun", "run-1"): SimpleNamespace(id="run-1", challenge_id="ch-1"),
            ("Challenge", "ch-1"): SimpleNamespace(
                id="ch-1", flag_pattern=r"flag\{[0-9a-f]{32}\}"
            ),
        }
    )
    factory = _SessionFactory(ledger)
    monkeypatch.setattr(module, "SessionLocal", factory)
    monkeypatch.setattr(module.flag_service, "record_runtime_flag", _record)

    poisoned = _PoisonedSession()
    # The helper must not need the Run's session at all; touching it raises.
    with __import__("pytest").raises(PendingRollbackError):
        poisoned.get(None, None)

    asyncio.run(
        RunSupervisor()._record_runtime_flag_isolated(
            "run-1", "flag{0123456789abcdef0123456789abcdef}"
        )
    )

    assert recorded == [("run-1", "flag{0123456789abcdef0123456789abcdef}")]
    # ``record_runtime_flag`` owns its own commit; the helper's job is only to
    # hand it a session that is not the Run's.
    assert factory.opened == 1


def test_a_lost_flag_is_reported_instead_of_swallowed(monkeypatch, caplog) -> None:
    events: list[tuple[str, dict]] = []

    ledger = _LedgerSession({})  # the Run row is gone
    monkeypatch.setattr(module, "SessionLocal", _SessionFactory(ledger))

    async def _append(session, run_id, event_type, payload=None):  # noqa: ANN001
        events.append((event_type, payload or {}))
        return SimpleNamespace(sequence=1)

    monkeypatch.setattr(module.event_service, "append", _append)

    with caplog.at_level("ERROR"):
        asyncio.run(
            RunSupervisor()._record_runtime_flag_isolated(
                "run-1", "flag{0123456789abcdef0123456789abcdef}"
            )
        )

    assert events and events[0][0] == "flag.ledger_write_failed"
    assert "flag ledger write FAILED" in caplog.text


def test_the_shared_session_cannot_write_the_ledger_once_poisoned() -> None:
    """Characterisation of the original bug, so the regression cannot come back.

    This is what ``_run_muteki`` used to do: hand the Run's own session to
    ``record_runtime_flag``.  Once a Worker thread has failed a flush on it, that
    call cannot even read ``challenge.flag_pattern``.
    """

    from app.services.flags import flag_service

    poisoned = _PoisonedSession()
    with __import__("pytest").raises(PendingRollbackError):
        asyncio.run(
            flag_service.record_runtime_flag(
                poisoned,
                SimpleNamespace(id="run-1", assistance_level="AUTONOMOUS"),
                SimpleNamespace(id="ch-1", flag_pattern=r"flag\{[0-9a-f]{32}\}"),
                "flag{0123456789abcdef0123456789abcdef}",
            )
        )


def test_a_suspected_flag_does_not_raise_or_emit_a_failure(monkeypatch) -> None:
    events: list[str] = []

    monkeypatch.setattr(module, "SessionLocal", _SessionFactory(_LedgerSession({})))

    async def _append(session, run_id, event_type, payload=None):  # noqa: ANN001
        events.append(event_type)
        return SimpleNamespace(sequence=1)

    monkeypatch.setattr(module.event_service, "append", _append)

    asyncio.run(
        RunSupervisor()._record_runtime_flag_isolated(
            "run-1", "flag{deadbeef}", required=False
        )
    )

    assert events == []
