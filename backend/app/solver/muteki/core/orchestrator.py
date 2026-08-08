from __future__ import annotations

from dataclasses import dataclass

from ..coordinator import CoordinatorConfig, MutekiCoordinator
from ..graph import MutekiGraph
from ..reason import MutekiReason
from ..workers import EngineProfile, MutekiWorkerPool


@dataclass(frozen=True, slots=True)
class MutekiRunResult:
    run_id: str
    challenge_id: str
    status: str
    flag_found: bool
    flag: str | None = None
    reason: str = ""
    graph_path: str = ""


class MutekiOrchestrator:
    """Compose the canonical graph, reasoner, worker pool and adapters.

    The worker callback is deliberately injected.  Production runtime uses
    the Tool/Runner/Evidence adapters; tests can provide a deterministic
    callback without importing the database or Runner implementation.
    """

    def __init__(
        self,
        graph: MutekiGraph,
        reason: MutekiReason,
        *,
        worker_runner,
        engines: list[EngineProfile] | None = None,
        max_workers: int = 10,
        interval_seconds: float = 0.0,
        engine_pool=None,
    ) -> None:
        self.graph = graph
        self.reason = reason
        self.engines = list(engines or [EngineProfile("gateway-runner")])
        self.pool = MutekiWorkerPool(graph, worker_runner, max_workers=max_workers, engine_pool=engine_pool)
        self.coordinator = MutekiCoordinator(
            graph,
            reason,
            self.pool,
            self.engines,
            config=CoordinatorConfig(max_workers=max_workers, interval_seconds=interval_seconds),
        )

    async def run(self, *, max_rounds: int = 10) -> MutekiRunResult:
        self.graph.emit_event(actor="muteki-runtime", event_type="run.started", payload={"max_rounds": max_rounds})
        try:
            await self.coordinator.run(max_ticks=max(0, int(max_rounds)))
            flags = self.graph.flags(verified_only=True)
            return MutekiRunResult(
                run_id=self.graph.challenge_id,
                challenge_id=self.graph.challenge_id,
                status="COMPLETED_SOLVED" if flags else "COMPLETED_UNSOLVED",
                flag=flags[-1].flag_value if flags else None,
                flag_found=bool(flags),
                reason="FLAG_VERIFIED" if flags else "NO_VERIFIED_FLAG",
                graph_path=str(self.graph.db_path),
            )
        except Exception as error:
            self.graph.add_dead_end(actor="muteki-runtime", description=f"runtime failure: {str(error)[:500]}")
            return MutekiRunResult(self.graph.challenge_id, self.graph.challenge_id, "FAILED_ENGINE", False, reason="MUTEKI_RUNTIME_ERROR", graph_path=str(self.graph.db_path))


__all__ = ["MutekiOrchestrator", "MutekiRunResult"]
