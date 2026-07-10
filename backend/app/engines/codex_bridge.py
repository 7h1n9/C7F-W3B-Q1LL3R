from collections.abc import AsyncIterator

import httpx

from app.engines.base import EngineEvent, SolveEngine


class CodexSdkEngine(SolveEngine):
    def __init__(self, bridge_url: str, workspace_path: str) -> None:
        self.bridge_url, self.workspace_path = bridge_url.rstrip("/"), workspace_path

    async def start(self, run_id: str) -> AsyncIterator[EngineEvent]:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(f"{self.bridge_url}/threads", json={"run_id": run_id, "workspace_path": self.workspace_path, "prompt": "Analyze only this authorized CTF workspace."})
            response.raise_for_status()
        yield EngineEvent("agent.message", {"message": "Codex thread created", **response.json()}, "ANALYZING")

    async def continue_run(self, run_id: str, message: str) -> AsyncIterator[EngineEvent]:
        yield EngineEvent("agent.message", {"message": message})

    async def cancel(self, run_id: str) -> None:
        return None

    async def resume(self, run_id: str) -> AsyncIterator[EngineEvent]:
        async for event in self.start(run_id):
            yield event
