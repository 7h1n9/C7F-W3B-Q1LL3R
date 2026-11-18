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
    #: Values the pipeline recovered that do NOT match this challenge's declared
    #: banner.  They are never a solve and are never submitted: they are handed
    #: to a human, because the usual cause is a value taken from somewhere other
    #: than the target (a public writeup, the original repository, the run's own
    #: notes).  Kept on the result so the Run can persist them as reviewable
    #: candidates instead of losing them.
    suspected_flags: tuple[str, ...] = ()


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
        stop_convergence_seconds: float | None = None,
        run_deadline_monotonic: float | None = None,
        exploit_first_action: str = "",
        exploit_first_action_source: str = "",
        exploit_first_action_verdict: str = "",
        exploit_probe_action: str = "",
        exploit_stop_loss_gate: str = "",
        technique_candidate_goal: str = "",
        insight_bus=None,
        evidence_reader=None,
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
                **(
                    {"stop_convergence_seconds": max(0.0, float(stop_convergence_seconds))}
                    if stop_convergence_seconds is not None
                    else {}
                ),
                **(
                    {"run_deadline_monotonic": float(run_deadline_monotonic)}
                    if run_deadline_monotonic is not None
                    else {}
                ),
                # The recipe's first move for this Challenge, resolved by the
                # Runtime (the Coordinator only sees the graph).  Passing it as a
                # keyword here is what makes it reach CoordinatorConfig -- this
                # constructor takes explicit parameters, so an unlisted keyword
                # raises TypeError and the whole Run dies as
                # MUTEKI_RUNTIME_ERROR with no traceback in the log.
                **({"exploit_first_action": str(exploit_first_action)} if exploit_first_action else {}),
                # Same routing hazard as above.  Provenance only feeds the
                # ``exploit.first_action`` telemetry event; it never changes the
                # goal, so an empty pair leaves the Run byte-identical.
                **(
                    {"exploit_first_action_source": str(exploit_first_action_source)}
                    if exploit_first_action_source
                    else {}
                ),
                **(
                    {"exploit_first_action_verdict": str(exploit_first_action_verdict)}
                    if exploit_first_action_verdict
                    else {}
                ),
                # Same routing hazard as above: an unlisted keyword here dies as
                # MUTEKI_RUNTIME_ERROR with no traceback.
                **(
                    {"exploit_stop_loss_gate": str(exploit_stop_loss_gate)}
                    if exploit_stop_loss_gate
                    else {}
                ),
                # Same routing hazard as above.  An unmeasured recipe's first move
                # -- a probe the Worker is told to verify, kept in a separate field
                # so it can never be mistaken for the flag-proven order above.
                **(
                    {"exploit_probe_action": str(exploit_probe_action)}
                    if exploit_probe_action
                    else {}
                ),
                # Same routing hazard as above.
                **(
                    {"technique_candidate_goal": str(technique_candidate_goal)}
                    if technique_candidate_goal
                    else {}
                ),
            ),
            official_worker_adapter=official_worker_adapter,
            official_worker_evidence_bridge=official_worker_evidence_bridge,
            official_worker_usage_bridge=official_worker_usage_bridge,
            insight_bus=insight_bus,
            # Retained tool output for the post-deadline flag recovery.  The
            # Coordinator cannot read it from the graph (the upstream backend
            # keeps structure, not tool text), so the Runtime supplies the
            # reader; ``None`` simply disables recovery.
            evidence_reader=evidence_reader,
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
            suspected = _suspected_flag_values(self.graph)
            stop_reason = self.coordinator.stop_reason
            if stop_reason in {
                "RACE_WORKER_TIMEOUT",
                "WORKER_TIMEOUT",
                "MUTEKI_RUN_TIMEOUT",
            }:
                if flags:
                    # The deadline is a runtime boundary, not evidence that a
                    # gate-verified flag disappeared from the Blackboard.
                    return MutekiRunResult(
                        run_id=self.graph.challenge_id,
                        challenge_id=self.graph.challenge_id,
                        status="COMPLETED_SOLVED",
                        flag=flags[-1].flag_value,
                        flag_found=True,
                        reason="FLAG_VERIFIED_AFTER_TIMEOUT",
                        graph_path=str(self.graph.db_path),
                        suspected_flags=suspected,
                    )
                return MutekiRunResult(
                    run_id=self.graph.challenge_id,
                    challenge_id=self.graph.challenge_id,
                    status="TIMEOUT",
                    flag_found=False,
                    reason=stop_reason,
                    graph_path=str(self.graph.db_path),
                    suspected_flags=suspected,
                )
            return MutekiRunResult(
                run_id=self.graph.challenge_id,
                challenge_id=self.graph.challenge_id,
                status="COMPLETED_SOLVED" if flags else "COMPLETED_UNSOLVED",
                flag=flags[-1].flag_value if flags else None,
                flag_found=bool(flags),
                reason="FLAG_VERIFIED" if flags else (stop_reason or "NO_VERIFIED_FLAG"),
                graph_path=str(self.graph.db_path),
                suspected_flags=suspected,
            )
        except Exception as error:
            self.graph.add_dead_end(actor="muteki-runtime", description=f"runtime failure: {str(error)[:500]}")
            return MutekiRunResult(self.graph.challenge_id, self.graph.challenge_id, "FAILED_ENGINE", False, reason="MUTEKI_RUNTIME_ERROR", graph_path=str(self.graph.db_path))


def _suspected_flag_values(graph) -> tuple[str, ...]:
    """Read the graph's "not a solve, ask a human" flags, in order, deduped.

    Best-effort: a graph backend that does not implement ``suspected_flags``
    simply reports none, which is the same behaviour as before this existed.
    """

    reader = getattr(graph, "suspected_flags", None)
    if not callable(reader):
        return ()
    try:
        values = [str(item.flag_value) for item in reader() if str(item.flag_value or "")]
    except Exception:  # noqa: BLE001 - a read convenience must never fail the Run
        return ()
    return tuple(dict.fromkeys(values))


__all__ = ["MutekiOrchestrator", "MutekiRunResult"]
