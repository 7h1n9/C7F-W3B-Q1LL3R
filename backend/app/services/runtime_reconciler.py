"""Periodic repair pass for durable execution state."""

from sqlalchemy import select

from app.models.run import SolveRun
from app.services.run_attempts import run_attempt_service
from app.services.run_finalizer import run_finalizer


async def reconcile_runtime_state(session, *, stale_after_seconds: int = 600) -> dict:
    """Reconcile every persisted run and recover abandoned execution rows.

    This pass is intentionally independent of the normal solve loop, so a
    process restart or a lost worker cannot leave RUNNING resources forever.
    """
    summary = {"runs": 0, "changed": 0, "stale_recovered": 0}
    runs = list((await session.scalars(select(SolveRun))).all())
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

