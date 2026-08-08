from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

from ...graph import Intent
from ..engine import WorkerEngine, WorkerResult, intent_prompt


class CodexEngine(WorkerEngine):
    """Adapter over the existing Codex bridge, with injectable client support."""

    def __init__(self, client=None, *, bridge_url: str | None = None) -> None:
        self.client = client
        self.bridge_url = (bridge_url or os.environ.get("CODEX_BRIDGE_URL") or "").rstrip("/")

    def engine_type(self) -> str:
        return "codex"

    def health_check(self) -> bool:
        return self.client is not None or bool(self.bridge_url)

    async def execute(self, intent: Intent, workspace: str) -> WorkerResult:
        if self.client is None and not self.bridge_url:
            return WorkerResult(False, self.engine_type(), metadata={"reason": "CODEX_BRIDGE_NOT_CONFIGURED"})
        try:
            client = self.client
            if client is None:
                from app.engines.codex_bridge import CodexSdkEngine

                client = CodexSdkEngine(self.bridge_url, workspace)
            run_id = str((intent.payload or {}).get("run_id") or intent.intent_id)
            events = client.start(run_id, intent_prompt(intent))
            output: list[str] = []
            async for event in _as_async_iterator(events):
                payload = getattr(event, "payload", {})
                text = payload.get("message") or payload.get("text") or payload.get("content")
                if text:
                    output.append(str(text))
            return WorkerResult(True, self.engine_type(), output="\n".join(output)[-12000:])
        except Exception as error:
            return WorkerResult(False, self.engine_type(), metadata={"reason": type(error).__name__, "error": str(error)[:500]})


async def _as_async_iterator(value) -> AsyncIterator[object]:
    if hasattr(value, "__aiter__"):
        async for item in value:
            yield item
        return
    if asyncio.iscoroutine(value):
        result = await value
        if result is not None:
            yield result


__all__ = ["CodexEngine"]
