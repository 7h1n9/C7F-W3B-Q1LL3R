import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.run import RunEvent
from app.orchestration.event_bus import event_bus


class EventService:
    def __init__(self) -> None:
        self._run_locks: dict[str, asyncio.Lock] = {}

    async def append(
        self, session: AsyncSession, run_id: str, event_type: str, payload: dict | None = None
    ) -> RunEvent:
        # This service runs in one backend process. A per-run lock works on SQLite
        # and the row lock/counter remains durable for production database sessions.
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            from app.models.run import SolveRun

            run = await session.scalar(
                select(SolveRun).where(SolveRun.id == run_id).with_for_update()
            )
            if run is None:
                raise ValueError("run not found")
            run.event_sequence += 1
            sequence = run.event_sequence
            event = RunEvent(
                run_id=run_id, sequence=sequence, event_type=event_type, payload_json=payload or {}
            )
            session.add(event)
            await session.flush()
            await session.commit()
            await session.refresh(event)
        await event_bus.publish(run_id, self.serialize(event))
        return event

    async def history(self, session: AsyncSession, run_id: str, after: int = 0) -> list[RunEvent]:
        return list(
            (
                await session.scalars(
                    select(RunEvent)
                    .where(RunEvent.run_id == run_id, RunEvent.sequence > after)
                    .order_by(RunEvent.sequence)
                )
            ).all()
        )

    @staticmethod
    def serialize(event: RunEvent) -> dict:
        return {
            "id": event.id,
            "run_id": event.run_id,
            "sequence": event.sequence,
            "event_type": event.event_type,
            "payload_json": event.payload_json,
            "created_at": event.created_at.isoformat(),
        }


event_service = EventService()
