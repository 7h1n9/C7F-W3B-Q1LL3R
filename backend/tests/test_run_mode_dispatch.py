"""Two ways a non-legacy Run used to become an unrecoverable zombie.

Both were found on the same Run (``da6493f7``): a canonical Muteki solve of
``BUU XXE`` that **had already found and had accepted** the flag, was parked in
``PAUSED_RECOVERY`` by a service restart (``SERVICE_RESTART_INTERRUPTED_TASK``),
and never moved again.

1. **Wrong controller.** A restart writes a durable ``CHECKPOINT_RECOVERY``
   continuation -- but the continuation executor called
   ``orchestrator.start`` directly, which is the *single-agent* controller.  It
   bootstrapped the Run, refreshed the legacy tool manifest, then died in
   ``build_engine`` on ``unsupported engine type: claude_cli``: a message that
   blames a CLI while the real fault is "this Run belongs to another solver
   mode".  ``run_supervisor.continue_until_terminal`` is the dispatcher that
   knows how to route by ``solver_mode``; it has to be the only door.

2. **A pause that could not fail.** ``PAUSED_RECOVERY`` allowed exactly
   ``{PLANNING, CANCELLED}``.  So once (1) raised a hard error, the error
   handler's own ``transition(run, FAILED_ENGINE)`` raised
   ``RUN_INVALID_STATE`` from *inside* the ``except`` -- the original cause was
   replaced by an invalid-transition error, the row kept its pause status with
   no lease and no task, and the supervisor re-enqueued it every 5s sweep into
   the same wall.  An error must never be harder to record than success:
   every non-terminal status can reach a terminal failure.

"""

from __future__ import annotations

import pytest

from app.orchestration.state_machine import ALLOWED, TERMINAL, RunStatus, transition

FAILURE_FINALS = (
    RunStatus.FAILED_ENGINE,
    RunStatus.FAILED_TOOL,
    RunStatus.FAILED_RUNNER,
    RunStatus.TIMEOUT,
    RunStatus.CANCELLED,
)


class _Run:
    """The minimum a transition touches."""

    def __init__(self, status: RunStatus) -> None:
        self.status = status.value
        self.current_phase = "INTAKE"
        self.started_at = None
        self.finished_at = None


NON_TERMINAL = [status for status in RunStatus if status not in TERMINAL]


def test_every_non_terminal_status_has_a_recordable_failure() -> None:
    """The property, stated once, so a new pause state cannot reintroduce the trap."""
    gaps = {
        status.value: sorted(
            target.value for target in FAILURE_FINALS if target not in ALLOWED.get(status, set())
        )
        for status in NON_TERMINAL
        if any(target not in ALLOWED.get(status, set()) for target in FAILURE_FINALS)
    }
    assert gaps == {}


@pytest.mark.parametrize("source", NON_TERMINAL, ids=lambda s: s.value)
@pytest.mark.parametrize("target", FAILURE_FINALS, ids=lambda s: s.value)
def test_transition_to_failure_is_accepted_from_any_non_terminal_status(
    source: RunStatus, target: RunStatus
) -> None:
    run = _Run(source)
    transition(run, target)
    assert run.status == target.value
    assert run.finished_at is not None, "a terminal failure must be stamped"


def test_paused_recovery_can_record_a_failure() -> None:
    """The exact dead end: a recovery attempt that fails must be able to say so."""
    run = _Run(RunStatus.PAUSED_RECOVERY)
    transition(run, RunStatus.FAILED_ENGINE)
    assert run.status == RunStatus.FAILED_ENGINE.value


def test_solved_runs_stay_immutable() -> None:
    """Widening the failure edges must not open the one status that is locked."""
    from app.core.exceptions import DomainError

    run = _Run(RunStatus.COMPLETED_SOLVED)
    with pytest.raises(DomainError) as caught:
        transition(run, RunStatus.FAILED_ENGINE)
    assert caught.value.code == "RUN_TERMINAL_IMMUTABLE"


# --------------------------------------------------------------------------- #
# Dispatch: the legacy controller must hand off a Run it does not own.
# --------------------------------------------------------------------------- #


class _ModeSession:
    """A session whose only job is to answer the one ``solver_mode`` read."""

    def __init__(self, mode: str | None) -> None:
        self._mode = mode
        self.commits = 0

    async def scalar(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        return self._mode

    async def commit(self) -> None:
        self.commits += 1

    async def __aenter__(self) -> _ModeSession:
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False


@pytest.fixture
def handoff(monkeypatch):  # noqa: ANN001
    """Wire ``orchestrator._handoff_non_legacy_mode`` to a recording dispatcher."""
    from app.orchestration import orchestrator as orchestrator_module
    from app.services import run_supervisor as run_supervisor_module

    recorded: list[tuple] = []

    async def fake_continue(session, run_id, user_message=None):  # noqa: ANN001
        recorded.append((run_id, user_message))
        return "outcome"

    monkeypatch.setattr(
        run_supervisor_module.run_supervisor, "continue_until_terminal", fake_continue
    )

    def install(mode: str | None) -> None:
        monkeypatch.setattr(
            orchestrator_module, "SessionLocal", lambda: _ModeSession(mode)
        )

    return orchestrator_module.orchestrator, install, recorded


@pytest.mark.parametrize("mode", ["muteki", "solver_v2", "MUTEKI"])
async def test_legacy_controller_hands_off_non_legacy_modes(handoff, mode: str) -> None:
    controller, install, recorded = handoff
    install(mode)

    assert await controller._handoff_non_legacy_mode("run-1", "hint") is True
    assert recorded == [("run-1", "hint")]


@pytest.mark.parametrize("mode", ["single_agent", None, ""])
async def test_legacy_controller_keeps_single_agent_runs(handoff, mode) -> None:
    controller, install, recorded = handoff
    install(mode)

    assert await controller._handoff_non_legacy_mode("run-1", None) is False
    assert recorded == []


# --------------------------------------------------------------------------- #
# The continuation executor must use the dispatcher, not the legacy controller.
# --------------------------------------------------------------------------- #


class _ContinuationService:
    def __init__(self, kind: str, run_id: str) -> None:
        self._kind = kind
        self._run_id = run_id
        self.completed: list[str] = []
        self.failed: list[str] = []

    async def claim(self, _session, continuation_id):  # noqa: ANN001
        return {
            "id": continuation_id,
            "kind": self._kind,
            "run_id": self._run_id,
            "payload": {"user_message": None},
        }

    async def complete(self, _session, continuation_id):  # noqa: ANN001
        self.completed.append(continuation_id)

    async def fail(self, _session, continuation_id, error):  # noqa: ANN001
        self.failed.append(continuation_id)


async def test_checkpoint_recovery_continuation_uses_the_dispatcher(monkeypatch) -> None:  # noqa: ANN001
    """A restart recovery must reach ``continue_until_terminal``.

    Reaching ``orchestrator.start`` instead is what produced
    ``unsupported engine type: claude_cli`` on a Muteki Run -- so pin the door,
    not just the routing behind it.
    """
    from app.orchestration import orchestrator as orchestrator_module
    from app.services import run_supervisor as run_supervisor_module
    from app.services.run_supervisor import run_supervisor

    routed: list[tuple] = []
    legacy_calls: list[tuple] = []

    async def fake_continue(session, run_id, user_message=None):  # noqa: ANN001
        routed.append((run_id, user_message))
        return "outcome"

    async def legacy_start(run_id, user_message=None):  # noqa: ANN001
        legacy_calls.append((run_id, user_message))

    monkeypatch.setattr(run_supervisor, "continue_until_terminal", fake_continue)
    monkeypatch.setattr(orchestrator_module.orchestrator, "start", legacy_start)
    monkeypatch.setattr(run_supervisor_module, "SessionLocal", lambda: _ModeSession(None))
    monkeypatch.setattr(
        run_supervisor_module,
        "continuation_service",
        _ContinuationService("CHECKPOINT_RECOVERY", "run-muteki"),
    )

    await run_supervisor._execute_continuation("cont-1")

    assert routed == [("run-muteki", None)]
    assert legacy_calls == [], "the legacy controller must not be entered directly"


# --------------------------------------------------------------------------- #
# A superseded arena Run must be closed, not left for the sweep to resume.
#
# This is the other half of the dispatch fix: routing a Muteki pause state to
# ``_run_muteki`` is correct *only* for a Run that is still current.  An arena
# retry abandons the Run it started, so without closing it the sweep would bring
# it back and two Runs would work the same challenge, one of them against a
# target the platform has already re-provisioned.
# --------------------------------------------------------------------------- #


class _RunRow:
    def __init__(self, status: str, solver_mode: str = "muteki") -> None:
        self.id = "run-1"
        self.status = status
        self.solver_mode = solver_mode


class _RunSession:
    def __init__(self, run) -> None:  # noqa: ANN001
        self._run = run

    async def get(self, _model, _key, **_kwargs):  # noqa: ANN002, ANN003
        return self._run

    async def __aenter__(self) -> _RunSession:
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False


@pytest.fixture
def abandon(monkeypatch):  # noqa: ANN001
    """Wire ``_abandon_run`` to recording stubs."""
    from app.services import arena_queue, run_lifecycle
    from app.services.run_supervisor import run_supervisor

    cancelled: list[tuple] = []
    handoffs: list[tuple] = []
    sessions: list[_RunSession] = []

    async def fake_cancel(_session, run_id, reason="Run cancelled by user."):  # noqa: ANN001
        cancelled.append((run_id, reason))

    async def fake_handoff(run_id, **_kwargs):  # noqa: ANN001
        handoffs.append((run_id,))
        return object()

    monkeypatch.setattr(run_lifecycle, "cancel_run", fake_cancel)
    monkeypatch.setattr(run_supervisor, "cancel_muteki_with_handoff", fake_handoff)

    def install(run):  # noqa: ANN001
        def factory():
            session = _RunSession(run)
            sessions.append(session)
            return session

        monkeypatch.setattr(arena_queue, "SessionLocal", factory)

    return arena_queue, install, cancelled, handoffs, sessions


async def test_abandon_run_closes_a_paused_muteki_run(abandon) -> None:
    """The orphan case: a Muteki Run parked in PAUSED_RECOVERY by an interruption."""
    module, install, cancelled, handoffs, _ = abandon
    install(_RunRow("PAUSED_RECOVERY"))

    await module._abandon_run("run-1", "superseded")

    assert handoffs == [("run-1",)], "a Muteki Run needs its own handoff first"
    assert cancelled == [("run-1", "superseded")]


async def test_abandon_run_skips_a_terminal_run(abandon) -> None:
    module, install, cancelled, handoffs, _ = abandon
    install(_RunRow("COMPLETED_SOLVED"))

    await module._abandon_run("run-1", "superseded")

    assert handoffs == []
    assert cancelled == [], "a finished Run must never be reopened as cancelled"


async def test_abandon_run_tolerates_an_unreadable_status(abandon) -> None:
    """An unknown status reads as "not terminal", so the row can still be closed."""
    module, install, cancelled, _, _ = abandon
    install(_RunRow("FAILED"))

    await module._abandon_run("run-1", "superseded")

    assert cancelled == [("run-1", "superseded")]


async def test_abandon_run_leaves_single_agent_runs_off_the_muteki_path(abandon) -> None:
    module, install, cancelled, handoffs, _ = abandon
    install(_RunRow("RUNNING", solver_mode="single_agent"))

    await module._abandon_run("run-1", "superseded")

    assert handoffs == []
    assert cancelled == [("run-1", "superseded")]


async def test_abandon_run_without_a_run_id_never_opens_a_session(abandon) -> None:
    module, install, cancelled, handoffs, sessions = abandon
    install(_RunRow("RUNNING"))

    await module._abandon_run(None, "superseded")

    assert sessions == [] and cancelled == [] and handoffs == []


# --------------------------------------------------------------------------- #
# The arena's "is this Run finished" check.
#
# ``arena_solve._is_terminal`` used a private prefix rule -- ``COMPLETED`` /
# ``FAILED`` only -- so it did not see ``CANCELLED``.  Cancelling a Run therefore
# left its queue item "in flight" for the full run timeout: the queue showed a
# healthy-looking item for twenty-five minutes after the operator had stopped it,
# the scheduler would not move on, and nothing said why.
# --------------------------------------------------------------------------- #


def test_the_arena_wait_agrees_with_the_state_machine() -> None:
    from app.services import arena_solve

    mismatches = {
        status.value: arena_solve._is_terminal(status.value)
        for status in RunStatus
        if arena_solve._is_terminal(status.value) != (status in TERMINAL)
    }
    assert mismatches == {}


def test_the_arena_wait_treats_cancelled_as_finished() -> None:
    """The exact regression: an operator cancel must end the wait immediately."""
    from app.services import arena_solve

    assert arena_solve._is_terminal("CANCELLED") is True
    assert arena_solve._is_terminal("TIMEOUT") is True
    assert arena_solve._is_terminal("POLICY_BLOCKED") is True


@pytest.mark.parametrize("unknown", ["FAILED", "COMPLETED", "", None, "RUNNING_"])
def test_the_arena_wait_keeps_waiting_on_an_unreadable_status(unknown: object) -> None:
    """Unknown reads as "not finished": keep waiting rather than declare victory."""
    from app.services import arena_solve

    assert arena_solve._is_terminal(unknown) is False


