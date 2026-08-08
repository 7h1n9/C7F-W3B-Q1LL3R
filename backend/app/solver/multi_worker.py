from __future__ import annotations

import asyncio
from enum import StrEnum
from typing import Any

from .reason import ReasonPlanner
from .shared_graph import SharedGraph, SolverEventBus
from .worker.pool import OneShotWorkerPool, WorkerJob, WorkerRole


class CoordinationPhase(StrEnum):
    PREPARE = "prepare"
    RACE = "race"
    COORDINATOR = "coordinator"
    FINALIZE = "finalize"


class MultiWorkerCoordinator:
    """Additive OODA scheduler for SharedGraph workers.

    It never writes the production Solver Blackboard and is not wired into
    ``SolverRuntimeService`` yet.  This keeps the new multi-worker experiment
    reversible while the contracts are validated independently.
    """

    def __init__(
        self,
        graph: SharedGraph,
        planner: ReasonPlanner,
        pool: OneShotWorkerPool,
        *,
        interval_seconds: float = 2.0,
        bootstrap_workers: int = 2,
        event_bus: SolverEventBus | None = None,
        run_id: str = "",
    ) -> None:
        self.graph = graph
        self.planner = planner
        self.pool = pool
        self.interval_seconds = max(0.0, interval_seconds)
        self.bootstrap_workers = max(0, bootstrap_workers)
        self.event_bus = event_bus
        self.run_id = run_id
        self.phase = CoordinationPhase.PREPARE
        self._last_revision: int | None = None
        self._worker_sequence = 0
        self._finished = False

    async def tick(self) -> bool:
        if self._finished:
            return False
        if self.graph.read_flags(verified_only=True):
            await self.finalize()
            return False
        if self.phase is CoordinationPhase.PREPARE:
            self._change_phase(CoordinationPhase.RACE)
            for _ in range(self.bootstrap_workers):
                await self._spawn(WorkerRole.BOOTSTRAP, None, "bootstrap target analysis")
            self._last_revision = self.graph.revision()
            return True
        revision = self.graph.revision()
        if revision == self._last_revision:
            return False
        self._change_phase(CoordinationPhase.COORDINATOR)
        snapshot = self.graph.snapshot()
        proposals = await self.planner.plan(snapshot)
        intent_ids = [
            self.graph.propose_intent(proposal.description, source_worker_id="coordinator")
            for proposal in proposals
        ]
        if proposals and self.pool.active_count < self.pool.max_workers:
            proposal = proposals[0]
            intent_id = intent_ids[0]
            await self._spawn(WorkerRole.EXPLORE, intent_id, proposal.description)
        self._last_revision = self.graph.revision()
        return True

    async def run(self, *, max_ticks: int = 1) -> None:
        for _ in range(max(0, max_ticks)):
            if not await self.tick():
                if self._finished:
                    break
            if self.interval_seconds:
                await asyncio.sleep(self.interval_seconds)
        if self._finished:
            await self.pool.wait()

    async def finalize(self) -> None:
        if self._finished:
            return
        self._change_phase(CoordinationPhase.FINALIZE)
        await self.pool.cancel_all()
        self._finished = True
        self._emit("RUN_FINISHED", status="solved" if self.graph.read_flags(verified_only=True) else "stopped")

    async def _spawn(self, role: WorkerRole, intent_id: str | None, description: str) -> bool:
        self._worker_sequence += 1
        job = WorkerJob(f"worker-{self._worker_sequence}", role, intent_id, description)
        return await self.pool.spawn(job)

    def _change_phase(self, phase: CoordinationPhase) -> None:
        if self.phase is phase:
            return
        self.phase = phase
        self._emit("PHASE_CHANGED", phase=phase.value)

    def _emit(self, event_type: str, **payload: Any) -> None:
        if self.event_bus:
            self.event_bus.emit(event_type, run_id=self.run_id, payload=payload)


__all__ = ["CoordinationPhase", "MultiWorkerCoordinator"]
