from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .events import EventType
from .graph import MutekiGraph
from .phases import MutekiPhase
from .reason import MutekiReason
from .workers import EngineProfile, MutekiWorkerPool, WorkerJob


@dataclass(frozen=True, slots=True)
class CoordinatorConfig:
    interval_seconds: float = 2.0
    max_workers: int = 10
    race_enabled: bool = True
    max_ticks: int = 100


class MutekiCoordinator:
    """Four-phase Muteki coordinator over one canonical graph."""

    def __init__(
        self,
        graph: MutekiGraph,
        reason: MutekiReason,
        pool: MutekiWorkerPool,
        engines: list[EngineProfile],
        *,
        config: CoordinatorConfig | None = None,
    ) -> None:
        self.graph = graph
        self.reason = reason
        self.pool = pool
        self.engines = engines
        self.config = config or CoordinatorConfig(max_workers=pool.max_workers)
        self.phase = MutekiPhase.PREPARE
        self._worker_number = 0
        self._last_revision = -1
        self._finalized = False
        self._stop_requested = False
        self._dispatched_intents: set[str] = set()

    async def run(self, *, max_ticks: int | None = None) -> None:
        limit = self.config.max_ticks if max_ticks is None else max(0, max_ticks)
        try:
            await self._prepare()
            if self.config.race_enabled:
                await self._race()
            if self.graph.flags(verified_only=True):
                return
            self._change_phase(MutekiPhase.COORDINATOR)
            for _ in range(limit):
                if self._stop_requested or self.graph.flags(verified_only=True):
                    break
                revision = self.graph.revision()
                if revision == self._last_revision:
                    if self.config.interval_seconds:
                        await asyncio.sleep(self.config.interval_seconds)
                    continue
                result = await self.reason.reason(self.graph)
                self.reason.write_intents(self.graph, result)
                await self._dispatch_open_intent()
                await asyncio.sleep(0)
                self._last_revision = self.graph.revision()
                if self.config.interval_seconds:
                    await asyncio.sleep(self.config.interval_seconds)
        finally:
            await self.finalize(reason="SOLVED" if self.graph.flags(verified_only=True) else "STOPPED")

    async def _prepare(self) -> None:
        self._change_phase(MutekiPhase.PREPARE)
        self.graph.emit_event(actor="coordinator", event_type=EventType.PHASE_CHANGED, payload={"phase": MutekiPhase.PREPARE.value})

    async def _race(self) -> None:
        self._change_phase(MutekiPhase.RACE)
        for profile in self.engines:
            if not profile.healthy:
                continue
            await self._spawn(profile, role="race")
        await self.pool.wait()

    async def _dispatch_open_intent(self) -> None:
        if self.pool.active_count >= self.pool.max_workers:
            return
        intents = [item for item in self.graph.intents(status="open")]
        intents = [item for item in intents if item.intent_id not in self._dispatched_intents]
        if not intents:
            return
        item = intents[0]
        profile = next((profile for profile in self.engines if profile.healthy), None)
        if profile is None:
            return
        if await self._spawn(profile, role="explore", intent_id=item.intent_id, goal=item.description, payload=item.payload or {}):
            self._dispatched_intents.add(item.intent_id)

    async def _spawn(self, profile: EngineProfile, *, role: str, intent_id: str | None = None, goal: str = "", payload: dict | None = None) -> bool:
        self._worker_number += 1
        job = WorkerJob(
            worker_id=f"worker-{self._worker_number}",
            role=role,
            engine_id=profile.engine_id,
            graph_path=str(self.graph.db_path),
            challenge_id=self.graph.challenge_id,
            intent_id=intent_id,
            goal=goal,
            payload=dict(payload or {}),
            environment={"MUTEKI_BLACKBOARD_DB": str(self.graph.db_path), "MUTEKI_WORKER_ID": f"worker-{self._worker_number}", "MUTEKI_CHALLENGE_ID": self.graph.challenge_id, **dict(profile.environment)},
        )
        return await self.pool.spawn(job)

    def request_stop(self) -> None:
        self._stop_requested = True

    async def finalize(self, *, reason: str) -> None:
        if self._finalized:
            return
        self._finalized = True
        self._change_phase(MutekiPhase.FINALIZE)
        await self.pool.cancel_all()
        self.graph.release_claims(actor="coordinator")
        self.graph.emit_event(actor="coordinator", event_type=EventType.RUN_FINISHED, payload={"reason": reason})

    def _change_phase(self, phase: MutekiPhase) -> None:
        if self.phase is phase:
            return
        self.phase = phase
        self.graph.emit_event(actor="coordinator", event_type=EventType.PHASE_CHANGED, payload={"phase": phase.value})


__all__ = ["CoordinatorConfig", "MutekiCoordinator"]
