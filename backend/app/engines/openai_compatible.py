import asyncio
from collections.abc import AsyncIterator

import httpx

from app.engines.base import EngineEvent, SolveEngine


class OpenAICompatibleEngine(SolveEngine):
    """Small, provider-neutral skeleton; orchestration and tools stay in the main backend."""
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 30.0) -> None:
        self.base_url, self.api_key, self.model, self.timeout = base_url.rstrip("/"), api_key, model, timeout

    async def start(self, run_id: str) -> AsyncIterator[EngineEvent]:
        yield EngineEvent("agent.message", {"message": "OpenAI-compatible engine is ready for orchestrated input."}, "ANALYZING")

    async def continue_run(self, run_id: str, message: str) -> AsyncIterator[EngineEvent]:
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(f"{self.base_url}/chat/completions", headers={"Authorization": f"Bearer {self.api_key}"}, json={"model": self.model, "messages": [{"role": "user", "content": message}]})
                    response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                yield EngineEvent("agent.message", {"message": content})
                return
            except (httpx.HTTPError, KeyError) as error:
                if attempt:
                    yield EngineEvent("run.failed", {"reason": str(error)}, "FAILED_ENGINE")
                else:
                    await asyncio.sleep(0.25)

    async def cancel(self, run_id: str) -> None:
        return None

    async def resume(self, run_id: str) -> AsyncIterator[EngineEvent]:
        async for event in self.start(run_id):
            yield event
