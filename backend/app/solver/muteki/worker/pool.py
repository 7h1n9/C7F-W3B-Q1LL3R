from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ..graph import Intent
from .engine import WorkerEngine, WorkerResult
from .engines import ClaudeEngine, CodexEngine, CursorEngine


@dataclass(frozen=True, slots=True)
class EngineWorker:
    engine: WorkerEngine
    intent: Intent

    async def execute(self, workspace: str) -> WorkerResult:
        return await self.engine.execute(self.intent, workspace)


class WorkerPool:
    """Heterogeneous engine registry with explicit busy-engine accounting."""

    def __init__(self, engines: dict[str, WorkerEngine] | None = None) -> None:
        self._engines = engines or {
            "codex": CodexEngine(),
            "claude": ClaudeEngine(),
            "cursor": CursorEngine(),
        }
        self._active: set[str] = set()
        self._lock = asyncio.Lock()

    @property
    def engines(self) -> dict[str, WorkerEngine]:
        return dict(self._engines)

    def get_available_engine(self, preferred: str | None = None) -> WorkerEngine | None:
        names = ([preferred] if preferred else []) + [name for name in self._engines if name != preferred]
        for name in names:
            engine = self._engines.get(name)
            if name not in self._active and engine is not None and engine.health_check():
                return engine
        return None

    async def acquire(self, preferred: str | None = None) -> WorkerEngine | None:
        async with self._lock:
            engine = self.get_available_engine(preferred)
            if engine is not None:
                self._active.add(engine.engine_type())
            return engine

    async def release(self, engine: WorkerEngine) -> None:
        async with self._lock:
            self._active.discard(engine.engine_type())

    def spawn_worker(self, intent: Intent, engine_type: str) -> EngineWorker:
        engine = self._engines.get(engine_type)
        if engine is None:
            raise KeyError(f"unknown worker engine: {engine_type}")
        return EngineWorker(engine, intent)

    async def execute(self, intent: Intent, workspace: str, *, preferred: str | None = None) -> WorkerResult:
        engine = await self.acquire(preferred)
        if engine is None:
            return WorkerResult(False, preferred or "none", metadata={"reason": "NO_HEALTHY_ENGINE"})
        try:
            return await engine.execute(intent, workspace)
        finally:
            await self.release(engine)


__all__ = ["EngineWorker", "WorkerPool"]
