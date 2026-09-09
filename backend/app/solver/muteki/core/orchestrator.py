from __future__ import annotations

from dataclasses import dataclass

from ..coordinator import CoordinatorConfig, MutekiCoordinator
from ..graph import MutekiGraph
from ..reason import MutekiReason
from ..worker.official_worker import OfficialWorkerAdapter
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
        worker_backend: str = "callback",
        official_worker_adapter: OfficialWorkerAdapter | None = None,
        official_worker_evidence_bridge=None,
        official_worker_usage_bridge=None,
        official_account_root: str | None = None,
        worker_timeout_seconds: int | None = None,
        worker_max_turns: int | None = None,
        max_total_workers: int | None = None,
        max_bootstrap_workers: int | None = None,
        max_idle_polls: int | None = None,
        insight_bus=None,
    ) -> None:
        self.graph = graph
        self.reason = reason
        default_engine = "codex" if worker_backend in {"upstream_local", "upstream_container"} else "gateway-runner"
        self.engines = list(engines or [EngineProfile(default_engine)])
        self.pool = MutekiWorkerPool(graph, worker_runner, max_workers=max_workers, engine_pool=engine_pool)
        self.coordinator = MutekiCoordinator(
            graph,
            reason,
            self.pool,
            self.engines,
            config=CoordinatorConfig(
                max_workers=max_workers,
                interval_seconds=interval_seconds,
                worker_backend=worker_backend,
                official_account_root=official_account_root,
                **(
                    {"worker_timeout_seconds": max(1, int(worker_timeout_seconds))}
                    if worker_timeout_seconds is not None
                    else {}
                ),
                **(
                    {"worker_max_turns": max(1, int(worker_max_turns))}
                    if worker_max_turns is not None
                    else {}
                ),
                **(
                    {"max_total_workers": max(1, int(max_total_workers))}
                    if max_total_workers is not None
                    else {}
                ),
                **(
                    {"max_bootstrap_workers": max(0, int(max_bootstrap_workers))}
                    if max_bootstrap_workers is not None
                    else {}
                ),
                **(
                    {"max_idle_polls": max(1, int(max_idle_polls))}
                    if max_idle_polls is not None
                    else {}
                ),
            ),
            official_worker_adapter=official_worker_adapter,
            official_worker_evidence_bridge=official_worker_evidence_bridge,
            official_worker_usage_bridge=official_worker_usage_bridge,
            insight_bus=insight_bus,
        )

    async def run(self, *, max_rounds: int | None = 10) -> MutekiRunResult:
        """Run the canonical loop until solved, stopped, or the outer budget ends.

        ``max_rounds`` is retained for focused callers and tests.  Production
        Runtime passes ``None`` so the Coordinator follows the upstream
        graph-driven semantics; the Run-level total-runtime deadline remains
        the safety boundary.  Agent/CLI turns belong to the Worker and must
        not be reused as a Coordinator tick limit.
        """

        self.graph.emit_event(
            actor="muteki-runtime",
            event_type="run.started",
            payload={"max_rounds": max_rounds},
        )
        try:
            if max_rounds is None:
                await self.coordinator.run(max_ticks=None, unbounded=True)
            else:
                await self.coordinator.run(max_ticks=max(0, int(max_rounds)))
            flags = self.graph.flags(verified_only=True)
            stop_reason = self.coordinator.stop_reason
            if stop_reason in {
                "RACE_WORKER_TIMEOUT",
                "WORKER_TIMEOUT",
                "MUTEKI_RUN_TIMEOUT",
            }:
                return MutekiRunResult(
                    run_id=self.graph.challenge_id,
                    challenge_id=self.graph.challenge_id,
                    status="TIMEOUT",
                    flag_found=False,
                    reason=stop_reason,
                    graph_path=str(self.graph.db_path),
                )
            return MutekiRunResult(
                run_id=self.graph.challenge_id,
                challenge_id=self.graph.challenge_id,
                status="COMPLETED_SOLVED" if flags else "COMPLETED_UNSOLVED",
                flag=flags[-1].flag_value if flags else None,
                flag_found=bool(flags),
                reason="FLAG_VERIFIED" if flags else (stop_reason or "NO_VERIFIED_FLAG"),
                graph_path=str(self.graph.db_path),
            )
        except Exception as error:
            self.graph.add_dead_end(actor="muteki-runtime", description=f"runtime failure: {str(error)[:500]}")
            return MutekiRunResult(self.graph.challenge_id, self.graph.challenge_id, "FAILED_ENGINE", False, reason="MUTEKI_RUNTIME_ERROR", graph_path=str(self.graph.db_path))


__all__ = ["MutekiOrchestrator", "MutekiRunResult"]
