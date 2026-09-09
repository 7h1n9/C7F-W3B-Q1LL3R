from __future__ import annotations

import asyncio

from app.solver.muteki.insight_bus import Insight, InsightBus, InsightKind


def test_subscriber_receives_fact():
    async def scenario():
        bus = InsightBus("test-run")
        q = bus.subscribe("worker-2")
        await bus.publish(Insight(kind=InsightKind.FACT, by="worker-1", text="port 80 is open"))
        ins = await asyncio.wait_for(q.get(), timeout=2)
        assert ins.kind == InsightKind.FACT
        assert ins.by == "worker-1"
        assert ins.text == "port 80 is open"

    asyncio.run(scenario())


def test_publisher_does_not_receive_own():
    async def scenario():
        bus = InsightBus("test-run")
        q = bus.subscribe("worker-1")
        await bus.publish(Insight(kind=InsightKind.FACT, by="worker-1", text="port 80 is open"))
        import time
        await asyncio.sleep(0.1)
        assert q.empty()

    asyncio.run(scenario())


def test_subscriber_receives_dead_end():
    async def scenario():
        bus = InsightBus("test-run")
        q = bus.subscribe("worker-2")
        await bus.publish(Insight(kind=InsightKind.DEAD_END, by="worker-1", text="sql not possible"))
        ins = await asyncio.wait_for(q.get(), timeout=2)
        assert ins.kind == InsightKind.DEAD_END

    asyncio.run(scenario())


def test_subscriber_receives_flag():
    async def scenario():
        bus = InsightBus("test-run")
        q = bus.subscribe("worker-2")
        await bus.publish(Insight(kind=InsightKind.FLAG, by="worker-1", text="flag{test}"))
        ins = await asyncio.wait_for(q.get(), timeout=2)
        assert ins.kind == InsightKind.FLAG

    asyncio.run(scenario())


def test_late_subscriber_gets_backlog():
    async def scenario():
        bus = InsightBus("test-run")
        await bus.publish(Insight(kind=InsightKind.FACT, by="worker-1", text="fact 1"))
        await bus.publish(Insight(kind=InsightKind.FACT, by="worker-1", text="fact 2"))
        q = bus.subscribe("worker-2")
        ins1 = await asyncio.wait_for(q.get(), timeout=2)
        ins2 = await asyncio.wait_for(q.get(), timeout=2)
        assert ins1.text == "fact 1"
        assert ins2.text == "fact 2"

    asyncio.run(scenario())


def test_duplicate_guidance_deduped():
    async def scenario():
        bus = InsightBus("test-run")
        q = bus.subscribe("worker-2")
        await bus.publish(Insight(kind=InsightKind.GUIDANCE, by="review", text="check login"))
        await bus.publish(Insight(kind=InsightKind.GUIDANCE, by="review", text="check login"))
        ins1 = await asyncio.wait_for(q.get(), timeout=2)
        import time
        await asyncio.sleep(0.1)
        assert q.empty()

    asyncio.run(scenario())


def test_flag_deduped():
    async def scenario():
        bus = InsightBus("test-run")
        q = bus.subscribe("worker-2")
        await bus.publish(Insight(kind=InsightKind.FLAG, by="worker-1", text="flag{test}"))
        await bus.publish(Insight(kind=InsightKind.FLAG, by="worker-2", text="flag{test}"))
        ins1 = await asyncio.wait_for(q.get(), timeout=2)
        await asyncio.sleep(0.1)
        assert q.empty()

    asyncio.run(scenario())


def test_unsubscribe():
    async def scenario():
        bus = InsightBus("test-run")
        q = bus.subscribe("worker-2")
        bus.unsubscribe("worker-2")
        await bus.publish(Insight(kind=InsightKind.FACT, by="worker-1", text="should not be seen"))
        await asyncio.sleep(0.1)
        assert q.empty()

    asyncio.run(scenario())


def test_history_bounded():
    async def scenario():
        bus = InsightBus("test-run")
        for i in range(1100):
            await bus.publish(Insight(kind=InsightKind.FACT, by="w1", text=f"fact {i}"))
        assert len(bus.history) <= 1000

    asyncio.run(scenario())
