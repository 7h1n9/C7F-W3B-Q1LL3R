"""Non-blocking compaction scheduling with a durable per-Run lease."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.core.database import SessionLocal
from app.models.run import CompactionLease, SolveRun
from app.schemas.compaction import CompactionDecisionAction
from app.services.compaction import compaction_service
from app.services.infrastructure import record_failure


class CompactionWorker:
    LEASE_SECONDS = 120

    def __init__(self, worker_id: str | None = None) -> None:
        self.worker_id = worker_id or f"compaction-{uuid.uuid4()}"

    async def _claim(self, session, run_id: str) -> CompactionLease | None:
        now = datetime.now(UTC)
        existing = await session.scalar(select(CompactionLease).where(CompactionLease.run_id == run_id).with_for_update())
        if existing:
            expires = existing.expires_at.replace(tzinfo=UTC) if existing.expires_at.tzinfo is None else existing.expires_at
            if expires > now and existing.worker_id != self.worker_id:
                await session.rollback()
                return None
            await session.delete(existing)
            await session.flush()
        lease = CompactionLease(
            run_id=run_id,
            worker_id=self.worker_id,
            lease_token=secrets.token_urlsafe(24),
            acquired_at=now,
            expires_at=now + timedelta(seconds=self.LEASE_SECONDS),
        )
        session.add(lease)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return None
        return lease

    async def run(self, run_id: str) -> None:
        async with SessionLocal() as session:
            lease = None
            try:
                run = await session.get(SolveRun, run_id)
                if not run:
                    return
                triggered, _ = await compaction_service.should_compact(session, run)
                safe, _ = await compaction_service.safe_point(session, run)
                if not triggered or not safe:
                    return
                lease = await self._claim(session, run_id)
                if not lease:
                    return
                # The deterministic decision is intentional: compaction is a
                # safety operation and cannot wait for a model response.
                await compaction_service.apply(
                    session,
                    run,
                    CompactionDecisionAction(compaction_reason="scheduled_threshold"),
                    reason="scheduled_threshold",
                )
            except Exception as error:
                with contextlib.suppress(Exception):
                    await session.rollback()
                    failed_run = await session.get(SolveRun, run_id)
                    if failed_run:
                        failed_run.compaction_status = "FAILED"
                        failed_run.last_error_code = "COMPACTION_FAILED"
                        failed_run.last_error_message = str(error)[:2000]
                        record_failure(failed_run, code="COMPACTION_FAILED", message=str(error), stage="COMPACTION")
                        await session.commit()
            finally:
                if lease is None:
                    return
                with contextlib.suppress(Exception):
                    await session.execute(delete(CompactionLease).where(CompactionLease.lease_token == lease.lease_token))
                    await session.commit()


class CompactionScheduler:
    def __init__(self) -> None:
        self.worker = CompactionWorker()
        self.tasks: dict[str, asyncio.Task] = {}

    def enqueue(self, run_id: str) -> None:
        task = self.tasks.get(run_id)
        if task and not task.done():
            return
        task = asyncio.create_task(self.worker.run(run_id))
        self.tasks[run_id] = task
        def done(completed: asyncio.Task) -> None:
            if self.tasks.get(run_id) is completed:
                self.tasks.pop(run_id, None)
        task.add_done_callback(done)


compaction_scheduler = CompactionScheduler()
