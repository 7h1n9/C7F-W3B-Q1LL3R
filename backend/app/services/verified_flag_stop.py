from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.run import Artifact, RunAttempt, RunExecutionLease, SolveRun
from app.services.events import event_service
from app.services.solver_state import solver_state_service


class VerifiedFlagStopController:
    """Stop event consumption/resume after verification without claiming SDK cancellation."""

    async def stop(self, session: AsyncSession, run: SolveRun, *, candidate_id: str | None = None, terminal_event_sequence: int | None = None) -> None:
        if run.status == "COMPLETED_SOLVED" and run.thread_invalidated:
            await solver_state_service.sync_from_run(session, run)
            return
        now = datetime.now(UTC)
        run.status = "COMPLETED_SOLVED"
        run.current_phase = "COMPLETED_SOLVED"
        run.finished_at = run.finished_at or now
        run.terminal_generation = int(run.terminal_generation or 0) + 1
        run.terminal_event_sequence = terminal_event_sequence or run.terminal_event_sequence
        run.thread_invalidated = True
        await session.flush()
        await solver_state_service.sync_from_run(session, run)
        attempts = list((await session.scalars(select(RunAttempt).where(RunAttempt.run_id == run.id, RunAttempt.status == "RUNNING"))).all())
        for attempt in attempts:
            attempt.status = "COMPLETED_SOLVED"
            attempt.finished_at = now
            attempt.error_code = None
        leases = list((await session.scalars(select(RunExecutionLease).where(RunExecutionLease.run_id == run.id))).all())
        for lease in leases:
            await session.delete(lease)
        reports = list((await session.scalars(select(Artifact).where(Artifact.run_id == run.id, Artifact.artifact_type == "report", Artifact.status == "ACTIVE"))).all())
        for report in reports:
            report.status = "STALE"
        await session.commit()
        await event_service.append(session, run.id, "run.generation_terminal", {"generation": run.terminal_generation, "outcome": "COMPLETED_SOLVED"})
        await event_service.append(session, run.id, "thread.invalidated", {"reason": "flag.verified", "candidate_id": candidate_id})
        await event_service.append(session, run.id, "lease.released", {"reason": "flag.verified", "count": len(leases)})

    async def is_stopped(self, session: AsyncSession, run_id: str) -> bool:
        run = await session.get(SolveRun, run_id)
        return bool(run and run.thread_invalidated)


verified_flag_stop_controller = VerifiedFlagStopController()
