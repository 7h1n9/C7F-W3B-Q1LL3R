from collections.abc import AsyncIterator

import httpx

from app.engines.base import EngineEvent, SolveEngine


class CodexSdkEngine(SolveEngine):
    def __init__(self, bridge_url: str, workspace_path: str) -> None:
        self.bridge_url, self.workspace_path = bridge_url.rstrip("/"), workspace_path
        self.thread_ids: dict[str, str] = {}

    async def start(self, run_id: str) -> AsyncIterator[EngineEvent]:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{self.bridge_url}/threads",
                json={
                    "run_id": run_id,
                    "workspace_path": self.workspace_path,
                    "prompt": "Analyze only this authorized CTF workspace.",
                },
            )
            response.raise_for_status()
        payload = response.json()
        thread_id = payload.get("thread_id")
        if not isinstance(thread_id, str):
            raise RuntimeError("Codex Bridge did not return a thread ID")
        self.thread_ids[run_id] = thread_id
        yield EngineEvent(
            "agent.message", {"message": "Codex thread created", **payload}, "ANALYZING"
        )

    async def continue_run(self, run_id: str, message: str) -> AsyncIterator[EngineEvent]:
        thread_id = self.thread_ids.get(run_id)
        if not thread_id:
            raise RuntimeError("Codex thread ID is unavailable")
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{self.bridge_url}/threads/{thread_id}/run", json={"prompt": message}
            )
            response.raise_for_status()
        for event in response.json().get("events", []):
            yield EngineEvent(
                str(event.get("type", "agent.message")), dict(event.get("payload", {}))
            )

    async def cancel(self, run_id: str) -> None:
        thread_id = self.thread_ids.get(run_id)
        if not thread_id:
            return
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(f"{self.bridge_url}/threads/{thread_id}/cancel")
            if response.status_code == 501:
                raise RuntimeError("CANCEL_NOT_SUPPORTED")
            response.raise_for_status()

    async def resume(self, run_id: str) -> AsyncIterator[EngineEvent]:
        thread_id = self.thread_ids.get(run_id)
        if not thread_id:
            async for event in self.start(run_id):
                yield event
            return
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{self.bridge_url}/threads/{thread_id}/resume",
                json={"prompt": "Resume the authorized analysis."},
            )
            response.raise_for_status()
        for event in response.json().get("events", []):
            yield EngineEvent(
                str(event.get("type", "agent.message")), dict(event.get("payload", {}))
            )
