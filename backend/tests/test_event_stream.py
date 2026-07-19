import asyncio

import pytest

from app.orchestration.event_bus import event_bus


@pytest.mark.asyncio
async def test_event_subscription_survives_heartbeat_timeout() -> None:
    run_id = "heartbeat-timeout-regression"
    subscription = event_bus.subscribe(run_id)
    pending_event = asyncio.create_task(anext(subscription))
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(pending_event), timeout=0.01)

        expected = {"sequence": 1, "event_type": "run.status_changed"}
        await event_bus.publish(run_id, expected)
        assert await pending_event == expected
    finally:
        if not pending_event.done():
            pending_event.cancel()
        await asyncio.gather(pending_event, return_exceptions=True)
        await subscription.aclose()
