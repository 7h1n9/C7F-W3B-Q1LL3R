"""The periodic reconciler must not sweep terminal Runs.

Measured 2026-10-25: ``cleanup_loop`` called ``reconcile_runtime_state`` every 10
seconds, and that pass selected **every** persisted Run regardless of status.
A terminal Run has nothing left to repair -- its leases, attempts and tool calls
were closed by the path that finished it -- so the pass only rewrote rows that
were already correct, forever, and made the arena's own history unreadable.
"""

from __future__ import annotations

from app.services.runtime_reconciler import _LIVE_STATUSES
from app.services.run_finalizer import TERMINAL_RUN_STATUSES


def test_the_sweep_never_selects_a_terminal_status() -> None:
    """A terminal Run has no durable execution state left to repair.

    Its leases, attempts and tool calls were cleaned by the path that closed it
    (``run_finalizer.reconcile`` on completion, or the startup sweep after a
    restart).  Sweeping it again can only rewrite rows that are already correct.
    """

    overlap = {str(status) for status in TERMINAL_RUN_STATUSES} & set(_LIVE_STATUSES)
    assert overlap == set(), f"terminal statuses must not be swept: {overlap}"


def test_the_sweep_still_covers_every_in_flight_status() -> None:
    """Restricting the sweep must not stop it repairing live Runs.

    The pass exists because a crashed process can leave a RUNNING row holding a
    lease forever.  Dropping an in-flight status would reopen exactly that hole,
    so the guard here is on the live side, not just the terminal one.
    """

    for status in (
        "CREATED",
        "PLANNING",
        "ANALYZING",
        "EXECUTING",
        "EVALUATING",
        "PAUSED_RECOVERY",
        "PAUSED_CHECKPOINT",
        "WAITING_USER",
        "RUNNING",
    ):
        assert status in _LIVE_STATUSES, status
