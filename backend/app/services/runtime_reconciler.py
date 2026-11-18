"""Periodic repair pass for durable execution state."""

from sqlalchemy import select

from app.models.run import SolveRun
from app.services.run_attempts import run_attempt_service
from app.services.run_finalizer import run_finalizer

#: Statuses that can still change, and therefore the only ones worth sweeping.
#:
#: The pass exists to repair *durable execution state* -- leaked leases, stale
#: attempts, a phase projection that drifted.  A Run in a terminal status has
#: none of that left to repair: ``run_finalizer.reconcile``'s terminal branch
#: has already closed every executable resource it owned, so sweeping it again
#: only rewrites rows that are already correct.
#:
#: Measured 2026-10-25: this pass ran every 10 seconds over the whole table
#: (448 rows) regardless of state, rewriting the same rows forever and making
#: the arena's own history unreadable.
#:
#: So the sweep is restricted to Runs that are still in flight.  A Run that has
#: reached a terminal status has already been reconciled by the path that put it
#: there (``run_finalizer.reconcile`` on completion, or the startup sweep after
#: a restart), and there is nothing left for a periodic pass to find.
_LIVE_STATUSES: tuple[str, ...] = (
    "CREATED",
    "PLANNING",
    "ANALYZING",
    "EXECUTING",
    "EVALUATING",
    "PAUSED_RECOVERY",
    "PAUSED_CHECKPOINT",
    "WAITING_USER",
    "RUNNING",
)


async def reconcile_runtime_state(session, *, stale_after_seconds: int = 600) -> dict:
    """Reconcile every *live* run and recover abandoned execution rows.

    This pass is intentionally independent of the normal solve loop, so a
    process restart or a lost worker cannot leave RUNNING resources forever.
    It deliberately does not touch terminal Runs -- see ``_LIVE_STATUSES``.
    """
    summary = {"runs": 0, "changed": 0, "stale_recovered": 0}
    runs = list(
        (
            await session.scalars(
                select(SolveRun).where(SolveRun.status.in_(_LIVE_STATUSES))
            )
        ).all()
    )
    for run in runs:
        summary["runs"] += 1
        before = (str(run.status), str(run.current_phase or ""), run.last_error_code)
        await run_finalizer.reconcile(session, run)
        if str(run.status) == "EXECUTING":
            if await run_attempt_service.recover_stale_execution(session, run, stale_after_seconds=stale_after_seconds):
                summary["stale_recovered"] += 1
        after = (str(run.status), str(run.current_phase or ""), run.last_error_code)
        if before != after:
            summary["changed"] += 1
    await session.commit()
    return summary

