from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.run import RunEvent
from app.orchestration.event_bus import event_bus


class EventService:
    async def append(self, session: AsyncSession, run_id: str, event_type: str, payload: dict | None = None) -> RunEvent:
        sequence = (await session.scalar(select(func.max(RunEvent.sequence)).where(RunEvent.run_id == run_id)) or 0) + 1
        event = RunEvent(run_id=run_id, sequence=sequence, event_type=event_type, payload_json=payload or {})
        session.add(event)
        await session.flush()
        await session.commit()
        await session.refresh(event)
        await event_bus.publish(run_id, self.serialize(event))
        return event

    async def history(self, session: AsyncSession, run_id: str, after: int = 0) -> list[RunEvent]:
        return list((await session.scalars(select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.sequence > after).order_by(RunEvent.sequence))).all())

    @staticmethod
    def serialize(event: RunEvent) -> dict:
        return {"id": event.id, "run_id": event.run_id, "sequence": event.sequence, "event_type": event.event_type, "payload_json": event.payload_json, "created_at": event.created_at.isoformat()}


event_service = EventService()
