"""Durable controller continuation requests."""

import socket
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.run import RunContinuation


CONTINUATION_PENDING = "PENDING"
CONTINUATION_RUNNING = "RUNNING"
CONTINUATION_COMPLETED = "COMPLETED"
CONTINUATION_FAILED = "FAILED"
ACTIVE_CONTINUATION_STATES = (CONTINUATION_PENDING, CONTINUATION_RUNNING)


def utc_now() -> datetime:
    return datetime.now(UTC)


class ContinuationService:
    owner_instance_id = f"{socket.gethostname()}:{__import__('os').getpid()}"

    async def request(
        self,
        session,
        run_id: str,
        *,
        kind: str,
        dedupe_key: str,
        payload: dict | None = None,
        attempt_id: str | None = None,
    ) -> RunContinuation:
        existing = await session.scalar(select(RunContinuation).where(
            RunContinuation.run_id == run_id,
            RunContinuation.dedupe_key == dedupe_key,
        ))
        if existing is not None:
            return existing
        item = RunContinuation(
            id=str(uuid.uuid4()),
            run_id=run_id,
            attempt_id=attempt_id,
            kind=kind,
            dedupe_key=dedupe_key,
            status=CONTINUATION_PENDING,
            payload_json=dict(payload or {}),
            available_at=utc_now(),
        )
        session.add(item)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            item = await session.scalar(select(RunContinuation).where(
                RunContinuation.run_id == run_id,
                RunContinuation.dedupe_key == dedupe_key,
            ))
            if item is None:
                raise
        return item

    async def request_committed(
        self,
        run_id: str,
        *,
        kind: str,
        dedupe_key: str,
        payload: dict | None = None,
        attempt_id: str | None = None,
    ) -> dict:
        """Create and commit a continuation outside the caller transaction."""
        from app.core.database import SessionLocal

        async with SessionLocal() as session:
            item = await self.request(
                session,
                run_id,
                kind=kind,
                dedupe_key=dedupe_key,
                payload=payload,
                attempt_id=attempt_id,
            )
            await session.commit()
            return {"id": item.id, "run_id": item.run_id, "status": item.status}

    async def recover_stale(self, session, *, stale_after_seconds: int = 300) -> int:
        cutoff = utc_now() - timedelta(seconds=stale_after_seconds)
        rows = list((await session.scalars(select(RunContinuation).where(
            RunContinuation.status == CONTINUATION_RUNNING,
            RunContinuation.claimed_at < cutoff,
        ))).all())
        for item in rows:
            item.status = CONTINUATION_PENDING
            item.owner_instance_id = None
            item.claimed_at = None
            item.available_at = utc_now()
        if rows:
            await session.commit()
        return len(rows)

    async def claim(self, session, continuation_id: str) -> RunContinuation | None:
        item = await session.scalar(select(RunContinuation).where(
            RunContinuation.id == continuation_id,
            RunContinuation.status == CONTINUATION_PENDING,
            RunContinuation.available_at <= utc_now(),
        ).with_for_update())
        if item is None:
            return None
        item.status = CONTINUATION_RUNNING
        item.owner_instance_id = self.owner_instance_id
        item.claimed_at = utc_now()
        item.attempts += 1
        await session.commit()
        return item

    async def complete(self, session, continuation_id: str) -> None:
        item = await session.get(RunContinuation, continuation_id)
        if item is None:
            return
        item.status = CONTINUATION_COMPLETED
        item.completed_at = utc_now()
        item.last_error_code = None
        item.last_error_message = None
        await session.commit()

    async def fail(self, session, continuation_id: str, error: Exception) -> None:
        item = await session.get(RunContinuation, continuation_id)
        if item is None:
            return
        item.status = CONTINUATION_FAILED
        item.last_error_code = getattr(error, "code", None) or type(error).__name__
        item.last_error_message = str(error)[:4000]
        await session.commit()

    async def pending(self, session) -> list[RunContinuation]:
        return list((await session.scalars(select(RunContinuation).where(
            RunContinuation.status.in_(ACTIVE_CONTINUATION_STATES),
            RunContinuation.available_at <= utc_now(),
        ).order_by(RunContinuation.created_at))).all())


continuation_service = ContinuationService()
