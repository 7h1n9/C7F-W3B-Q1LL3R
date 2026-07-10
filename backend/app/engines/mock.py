import asyncio
from collections.abc import AsyncIterator

from app.engines.base import EngineEvent, SolveEngine


class MockSolveEngine(SolveEngine):
    async def start(self, run_id: str) -> AsyncIterator[EngineEvent]:
        for event in (
            EngineEvent("agent.message", {"message": "Mock engine began authorized challenge analysis."}, "ANALYZING"),
            EngineEvent("agent.plan_created", {"steps": ["Inspect challenge metadata", "Request allowed target only", "Record evidence"]}, "PLANNING"),
            EngineEvent("agent.hypothesis_created", {"title": "Initial authorized surface review", "confidence": 20}, "EXECUTING"),
            EngineEvent("agent.message", {"message": "Mock evaluation found no verified flag."}, "EVALUATING"),
            EngineEvent("report.started", {"mode": "mock"}, "REPORTING"),
            EngineEvent("report.completed", {"mode": "mock"}, "REPORTING"),
            EngineEvent("run.completed", {"result": "Mock run finished without attempting exploitation."}, "COMPLETED_UNSOLVED"),
        ):
            await asyncio.sleep(0)
            yield event

    async def continue_run(self, run_id: str, message: str) -> AsyncIterator[EngineEvent]:
        yield EngineEvent("agent.message", {"message": f"Mock continuation recorded: {message}"})

    async def cancel(self, run_id: str) -> None:
        return None

    async def resume(self, run_id: str) -> AsyncIterator[EngineEvent]:
        async for event in self.start(run_id):
            yield event
