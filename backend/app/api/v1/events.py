import asyncio
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.orchestration.event_bus import event_bus
from app.services.events import event_service

router = APIRouter(prefix="/runs", tags=["events"])


@router.get("/{run_id}/events")
async def stream_events(run_id: str, request: Request, after: int = 0, session: AsyncSession = Depends(get_session)) -> StreamingResponse:
    async def event_stream():
        for event in await event_service.history(session, run_id, after):
            yield f"id: {event.sequence}\nevent: {event.event_type}\ndata: {json.dumps(event_service.serialize(event))}\n\n"
        subscription = event_bus.subscribe(run_id)
        while not await request.is_disconnected():
            try:
                event = await asyncio.wait_for(anext(subscription), timeout=15)
                yield f"id: {event['sequence']}\nevent: {event['event_type']}\ndata: {json.dumps(event)}\n\n"
            except TimeoutError:
                yield ": heartbeat\n\n"
    return StreamingResponse(event_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
