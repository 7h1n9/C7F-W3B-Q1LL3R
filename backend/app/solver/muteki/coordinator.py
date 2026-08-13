from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping

from .adapter.official_observation import (
    SafeNativeObservation,
    extract_native_observations,
)
from .container_exec import ContainerExecutor
from .control import ControlReceiver
from .core.stage_policy import StagePolicy
from .events import EventType
from .graph import MutekiGraph
from .outcomes import (
    WorkerResultCode,
    failure_code,
    signal_from_worker_text,
    stable_branch_id,
    stable_route_hash,
)
from .phases import MutekiPhase
from .reason import MutekiReason, ReasonResult
from .semantic_dispatch import (
    SemanticReservation,
    acquire_dispatch_semantics,
    release_dispatch_semantics,
)
from .worker.official_worker import (
    OfficialWorkerAdapter,
    OfficialWorkerConfig,
    OfficialWorkerResult,
)
from .worker.review_worker import ReviewWorker
from .workers import EngineProfile, MutekiWorkerPool, WorkerJob, WorkerOutcome

if TYPE_CHECKING:
    from .cli_driver import CLIDriver


_RACE_LANES: tuple[dict[str, str], ...] = (
    {
        "lane_key": "recon:public-endpoints",
        "label": "public endpoints and framework surface",
    },
    {
        "lane_key": "recon:session-api",
        "label": "session, authentication and API surface",
    },
    {
        "lane_key": "recon:business-objects",
        "label": "business objects, identifiers and workflow surface",
    },
)


@dataclass(frozen=True, slots=True)
class CoordinatorConfig:
    interval_seconds: float = 2.0
    max_workers: int = 10
    race_enabled: bool = True
    # Explicit bounded callers may still set this value.  Production Runtime
    # passes ``unbounded=True`` and uses the Run-level total-runtime deadline,
    # matching upstream Muteki's graph-driven Coordinator loop.
    max_ticks: int = 100
    review_interval: int = 3
    stage_policy: StagePolicy | dict | None = None
    worker_backend: str = "callback"
    worker_timeout_seconds: int = 900
    # Per-Worker CLI turns remain the native Muteki Worker budget.  The
    # Coordinator-wide spawn budget below is separate, just like upstream.
    worker_max_turns: int = 80
    # Upstream Muteki charges every newly spawned Worker against
    # ``max_total_workers`` before it starts the Worker task.  Keep the same
    # explicit boundary in the compatibility Coordinator so a production Run
    # cannot turn a barren route into an unbounded bootstrap loop.
    max_total_workers: int | None = None
    # The Worker owns the execution deadline.  Keep a small separate grace
    # window for process teardown/control-plane reporting so a result produced
    # exactly at the Worker deadline is not misclassified as a Coordinator
    # timeout.
    worker_wait_grace_seconds: float = 5.0
    official_account_root: str | None = None
    # Official Muteki keeps re-bootstrap bounded by Coordinator policy.  The
    # compatibility runtime exposes the same safety boundary.  Zero means
    # "no separate bootstrap cap"; the outer tick/runtime budget remains the
    # safety boundary.  This matches the upstream Coordinator, which keeps
    # re-bootstrap available after a barren route instead of silently ending
    # the solve after one whole-challenge Worker.
    max_bootstrap_workers: int = 0
    # A revision-less poll is an asynchronous wait, not a consumed reasoning
    # turn.  Keep a finite guard for a genuinely dead compatibility backend,
    # while allowing a just-started native Worker enough time to publish its
    # next Blackboard event.
    max_idle_polls: int = 30


class MutekiCoordinator:
    """Four-phase Muteki coordinator over one canonical graph."""

    def __init__(
        self,
        graph: MutekiGraph,
        reason: MutekiReason,
        pool: MutekiWorkerPool,
        engines: list[EngineProfile],
        *,
        config: CoordinatorConfig | dict | None = None,
        cli_driver: "CLIDriver | None" = None,
        container_executor: ContainerExecutor | None = None,
        control_receiver: ControlReceiver | None = None,
        official_worker_adapter: OfficialWorkerAdapter | None = None,
        official_worker_evidence_bridge=None,
        official_worker_usage_bridge=None,
    ) -> None:
        self.graph = graph
        self.reason = reason
        self.pool = pool
        self.engines = engines
        self.config = CoordinatorConfig(**config) if isinstance(config, dict) else (config or CoordinatorConfig(max_workers=pool.max_workers))
        self.stage_policy = StagePolicy.from_config(self.config.stage_policy)
        if self.config.worker_backend not in {
            "callback",
            "cli",
            "container",
            "upstream_local",
            "upstream_container",
        }:
            raise ValueError(f"unsupported Muteki worker backend: {self.config.worker_backend}")
        if (
            self.config.worker_backend in {"upstream_local", "upstream_container"}
            and not callable(getattr(graph, "native_graph", None))
        ):
            raise ValueError(
                "upstream native Worker/Sandbox requires "
                "APP_MUTEKI_GRAPH_BACKEND=upstream"
            )
        if self.config.worker_backend == "cli" and cli_driver is None:
            from .cli_driver import CLIDriver

            cli_driver = CLIDriver(timeout_seconds=self.config.worker_timeout_seconds)
        self.cli_driver = cli_driver
        self.container_executor = container_executor or (ContainerExecutor(timeout_seconds=self.config.worker_timeout_seconds) if self.config.worker_backend == "container" else None)
        self.control_receiver = control_receiver
        self.official_worker: OfficialWorkerAdapter | None = official_worker_adapter
        self.official_worker_usage_bridge = official_worker_usage_bridge
        if self.config.worker_backend in {"upstream_local", "upstream_container"}:
            if self.official_worker is None:
                native_graph = None
                native_graph_factory = getattr(graph, "native_graph", None)
                if callable(native_graph_factory):
                    native_graph = native_graph_factory()
                self.official_worker = OfficialWorkerAdapter(
                    OfficialWorkerConfig(
                        backend="container"
                        if self.config.worker_backend == "upstream_container"
                        else "local",
                        timeout_seconds=self.config.worker_timeout_seconds,
                        max_turns=max(1, int(self.config.worker_max_turns)),
                        # Keep the native Worker on the same explicitly
                        # authorized Docker network as the existing container
                        # execution path.  The default remains bridge for
                        # callers that do not opt into a target network.
                        network=os.environ.get("MUTEKI_CONTAINER_NETWORK", "bridge"),
                        protocol="cli_solver",
                    ),
                    step_callback=self._record_worker_step,
                    evidence_bridge=official_worker_evidence_bridge,
                    usage_bridge=official_worker_usage_bridge,
                    shared_graph=native_graph,
                )
            self.pool.external_runner = self._official_worker_runner
            # The official native Worker/Sandbox is the complete execution
            # boundary.  Do not let selected local-model profiles or a
            # compatibility callback fall through to ToolGateway/Kali Runner.
            self.pool.external_runner_exclusive = True
        if self.pool.review_handler is None:
            self.pool.review_handler = self._review_runner
        if self.config.worker_backend in {"cli", "container"}:
            self.pool.external_runner = self._external_worker_runner
        self.phase = MutekiPhase.PREPARE
        self._worker_number = 0
        self._last_revision = -1
        self._finalized = False
        self._stop_requested = False
        self._dispatched_intents: set[str] = set()
        self._semantic_reservations: dict[str, SemanticReservation] = {}
        self._active_route_hashes: set[str] = set()
        self._bootstrap_workers = 0
        self._spawned_workers = 0
        self._budget_exhausted = False
        self._stop_reason: str | None = None
        self._unavailable_engine_ids: set[str] = set()
        # A failed Prepare probe is a temporary availability state.  Keep the
        # engine out of the current dispatch round, but re-probe it in the
        # background after Worker rounds instead of permanently skipping it.
        self._health_retry_task: asyncio.Task[None] | None = None
        self._health_retry_attempts: dict[str, int] = {}
        self._reason_health_retry_task: asyncio.Task[None] | None = None
        self._reason_health_retry_attempts = 0
        self._engine_cursor = 0

    @property
    def stop_reason(self) -> str | None:
        """Return a controlled stop reason produced during this Run."""

        return self._stop_reason

    async def run(self, *, max_ticks: int | None = None, unbounded: bool = False) -> None:
        limit = None if unbounded else self.config.max_ticks if max_ticks is None else max(0, max_ticks)
        try:
            await self._prepare()
            if self.config.race_enabled:
                await self._race()
            if self._stop_requested or self.graph.flags(verified_only=True):
                return
            self._change_phase(MutekiPhase.COORDINATOR)
            tick = 0
            idle_polls = 0
            while limit is None or tick < limit:
                if self._stop_requested or self.graph.flags(verified_only=True):
                    break
                revision = self.graph.revision()
                if revision == self._last_revision:
                    # A revision-less poll is not an OODA turn.  The official
                    # Muteki Coordinator waits for an event/Graph change and
                    # only then asks Reason for another Intent.  Consuming the
                    # finite turn budget here was the source of premature
                    # STOPPED runs when a container Worker was still starting.
                    if (
                        not self.pool.active_count
                        and not self.graph.flags(verified_only=True)
                    ):
                        # An open Intent is already a Coordinator decision.
                        # Consume it before the whole-challenge recovery path;
                        # upstream Muteki never replaces a claimable route with
                        # a fresh bootstrap merely because the poll was idle.
                        before_open = self.pool.active_count
                        await self._dispatch_open_intent()
                        if self.pool.active_count > before_open:
                            await self._wait_for_worker_round()
                            tick += 1
                            idle_polls = 0
                            continue
                        before_bootstrap = self._bootstrap_workers
                        await self._dispatch_bootstrap()
                        if self._bootstrap_workers > before_bootstrap:
                            # A bootstrap is a real Worker round even when the
                            # native Worker only reports a dead-end and the
                            # facade has not observed a new upstream event yet.
                            # Wait for that round before charging one safety
                            # tick; otherwise an unlimited bootstrap policy can
                            # repeatedly spawn from this idle branch forever.
                            await self._wait_for_worker_round()
                            tick += 1
                            idle_polls = 0
                            continue
                    # If all currently selected engines are unavailable, keep
                    # the reactivation probe alive even though no Worker round
                    # can be dispatched yet.  This is local to this Run and
                    # never blocks another Run's Coordinator.
                    self._schedule_health_retry()
                    self._schedule_reason_health_retry()
                    idle_polls += 1
                    if idle_polls >= max(1, int(self.config.max_idle_polls)):
                        self.graph.add_dead_end(
                            actor="coordinator",
                            description="NO_GRAPH_PROGRESS",
                        )
                        break
                    await asyncio.sleep(max(0.01, self.config.interval_seconds))
                    continue
                idle_polls = 0
                tick += 1
                # Consume this snapshot before dispatching.  Worker facts
                # written during this turn must remain a new revision for the
                # following turn; assigning the post-worker revision here
                # would skip the next Reason pass entirely.
                self._last_revision = revision
                self.graph.emit_event(
                    actor="coordinator",
                    event_type=EventType.REASON_STARTED,
                    payload={
                        "phase": self.phase.value,
                        "revision": revision,
                        "active_workers": self.pool.active_count,
                        **self._reason_provider_telemetry(),
                    },
                )
                try:
                    result = await self.reason.reason(self.graph)
                except Exception as error:
                    # A Reason provider outage must remain observable and must
                    # not make the Coordinator disappear before it can choose
                    # a recovery/bootstrap turn.
                    result = ReasonResult(
                        False,
                        (),
                        verdict="course_correct",
                        drift="REASON_PROVIDER_FAILED",
                    )
                    self.graph.emit_event(
                        actor="coordinator",
                        event_type=EventType.REASON_FAILED,
                        payload={
                            "phase": self.phase.value,
                            "revision": revision,
                            "reason_code": type(error).__name__.upper()[:80],
                        },
                    )
                else:
                    self.graph.emit_event(
                        actor="coordinator",
                        event_type=EventType.REASON_COMPLETED,
                        payload={
                            "phase": self.phase.value,
                            "revision": revision,
                            "verdict": str(result.verdict),
                            "goal_met": bool(result.goal_met),
                            "intent_count": len(result.intents),
                            **self._reason_provider_telemetry(),
                        },
                    )
                self.reason.write_intents(self.graph, result)
                await self._dispatch_open_intent()
                # Official Muteki treats ``course_correct`` as a scheduling
                # decision. Reuse the existing Review Worker and StagePolicy
                # so the next branch is selected from the canonical Graph.
                # Do not copy the model's drift prose into audit or Worker
                # payloads; the Review Worker reads the Graph directly.
                if result.verdict == "course_correct":
                    self.graph.emit_event(
                        actor="coordinator",
                        event_type=EventType.COORDINATOR_DIRECTIVE,
                        payload={"directive": "course_correct", "review_requested": True},
                    )
                    await self._dispatch_review(goal="review course-corrected route")
                if (
                    not result.intents
                    and not self.pool.active_count
                    and not self.graph.flags(verified_only=True)
                ):
                    await self._dispatch_bootstrap()
                await self._wait_for_worker_round()
                if self.config.review_interval > 0 and tick % self.config.review_interval == 0:
                    await self._dispatch_review()
                await asyncio.sleep(0)
                if self.config.interval_seconds:
                    await asyncio.sleep(self.config.interval_seconds)
        finally:
            await self.finalize(reason="SOLVED" if self.graph.flags(verified_only=True) else "STOPPED")

    async def _wait_for_worker_round(self) -> None:
        """Wait for one Worker round and schedule engine reactivation.

        Engine health recovery is deliberately fire-and-forget within this
        Run.  The next Worker round is allowed to continue while the failed
        engine is probed; the probe itself waits until no Worker is active so
        it cannot compete with an execution window in the same Sandbox.
        """

        if not self.pool.active_count:
            self._schedule_health_retry()
            self._schedule_reason_health_retry()
            return
        try:
            await asyncio.wait_for(
                self.pool.wait(),
                timeout=max(
                    1.0,
                    float(self.config.worker_timeout_seconds)
                    + max(0.0, float(self.config.worker_wait_grace_seconds)),
                ),
            )
        except asyncio.TimeoutError:
            self.graph.add_dead_end(actor="coordinator", description="WORKER_TIMEOUT")
            await self._recover_timed_out_workers()
        self._release_completed_semantics()
        self._schedule_health_retry()
        self._schedule_reason_health_retry()

    def _schedule_reason_health_retry(self) -> None:
        """Probe an unavailable Coordinator Reason model asynchronously."""

        provider = getattr(self.reason, "provider", None)
        if (
            self._finalized
            or provider is None
            or not bool(getattr(provider, "needs_reactivation", False))
            or (
                self._reason_health_retry_task is not None
                and not self._reason_health_retry_task.done()
            )
        ):
            return
        health = getattr(provider, "health", None)
        if not callable(health):
            return
        self._reason_health_retry_task = asyncio.create_task(
            self._retry_reason_model(provider, health),
            name=f"muteki-reason-health-retry-{self.graph.challenge_id}",
        )

    async def _retry_reason_model(self, provider, health) -> None:
        """Re-probe Reason without blocking Worker dispatch or other Runs."""

        self._reason_health_retry_attempts += 1
        attempt = self._reason_health_retry_attempts
        try:
            healthy, reason_code = await health()
        except Exception:
            healthy, reason_code = False, "REASON_HEALTHCHECK_FAILED"
        payload = {
            "attempt": attempt,
            "healthy": bool(healthy),
            "reason_code": _safe_worker_reason(reason_code)
            or ("HEALTHY" if healthy else "REASON_HEALTHCHECK_FAILED"),
            "reactivated": bool(healthy),
            "source": "coordinator_reason_model",
        }
        self.graph.emit_event(
            actor="coordinator",
            event_type=(
                EventType.REASON_MODEL_REACTIVATED
                if healthy
                else EventType.REASON_MODEL_FALLBACK
            ),
            payload=payload,
        )

    def _schedule_health_retry(self) -> None:
        """Schedule one non-blocking reactivation probe for this Run."""

        if (
            self._finalized
            or self.official_worker is None
            or not self._unavailable_engine_ids
            or (
                self._health_retry_task is not None
                and not self._health_retry_task.done()
            )
        ):
            return
        self._health_retry_task = asyncio.create_task(
            self._retry_unavailable_engines(),
            name=f"muteki-health-retry-{self.graph.challenge_id}",
        )

    async def _retry_unavailable_engines(self) -> None:
        """Re-probe failed engines and return recovered profiles to dispatch."""

        health = getattr(self.official_worker, "health", None)
        if not callable(health):
            return
        # Do not run a health process against the same Sandbox while another
        # Worker is active.  The task remains independent from the Worker and
        # resumes as soon as the current round has released the Sandbox.
        while self.pool.active_count and not self._stop_requested:
            await asyncio.sleep(0.05)
        if self._stop_requested or self._finalized:
            return
        profiles = [
            profile
            for profile in self.engines
            if profile.engine_id in self._unavailable_engine_ids
            and str((profile.driver_profile or {}).get("protocol") or "")
            != "chat_completions"
        ]
        if not profiles:
            return
        workspace = str(self.graph.db_path.parent.parent)

        async def check(profile: EngineProfile) -> None:
            attempt = self._health_retry_attempts.get(profile.engine_id, 0) + 1
            self._health_retry_attempts[profile.engine_id] = attempt
            try:
                healthy, reason = await health(profile, workspace=workspace)
            except Exception:
                healthy, reason = False, "WORKER_HEALTHCHECK_FAILED"
            healthy = bool(healthy)
            reason_code = _safe_worker_reason(reason) or (
                "HEALTHY" if healthy else "WORKER_HEALTHCHECK_FAILED"
            )
            self.graph.emit_event(
                actor="coordinator",
                event_type=EventType.PREPARE_ENGINE_CHECKED,
                payload={
                    "engine_id": profile.engine_id,
                    "healthy": healthy,
                    "reason_code": reason_code,
                    "source": "worker_sandbox",
                    "retry": True,
                    "attempt": attempt,
                    "reactivated": healthy,
                },
            )
            if healthy:
                self._unavailable_engine_ids.discard(profile.engine_id)

        await asyncio.gather(*(check(profile) for profile in profiles))

    async def _prepare(self) -> None:
        self._change_phase(MutekiPhase.PREPARE)
        self.graph.emit_event(actor="coordinator", event_type=EventType.PHASE_CHANGED, payload={"phase": MutekiPhase.PREPARE.value})
        if self.official_worker is not None:
            await self.official_worker.start(
                run_id=self.graph.challenge_id,
                workspace=str(self.graph.db_path.parent.parent),
                account_root=self.config.official_account_root,
            )
            await self._check_official_worker_health()

    async def _check_official_worker_health(self) -> None:
        """Probe native CLI profiles before the first Race Worker.

        The upstream Muteki Swarm treats a real one-turn health result as a
        dispatch prerequisite.  Keep that boundary at the production adapter:
        Chat-Completions profiles are handled by their own Worker protocol and
        are therefore not sent through the CLI probe, while CLI profiles are
        tested inside the run Sandbox with the same projected credentials as a
        real Worker.
        """

        if self.official_worker is None:
            return
        health = getattr(self.official_worker, "health", None)
        if not callable(health):
            return
        profiles = [
            profile
            for profile in self.engines
            if profile.healthy
            and str((profile.driver_profile or {}).get("protocol") or "")
            != "chat_completions"
        ]
        if not profiles:
            return

        workspace = str(self.graph.db_path.parent.parent)

        async def check(profile: EngineProfile) -> None:
            try:
                healthy, reason = await health(profile, workspace=workspace)
            except Exception:
                healthy, reason = False, "WORKER_HEALTHCHECK_FAILED"
            healthy = bool(healthy)
            reason_code = _safe_worker_reason(reason) or (
                "HEALTHY" if healthy else "WORKER_HEALTHCHECK_FAILED"
            )
            self.graph.emit_event(
                actor="coordinator",
                event_type=EventType.PREPARE_ENGINE_CHECKED,
                payload={
                    "engine_id": profile.engine_id,
                    "healthy": healthy,
                    "reason_code": reason_code,
                    "source": "worker_sandbox",
                },
            )
            if not healthy:
                self._unavailable_engine_ids.add(profile.engine_id)

        await asyncio.gather(*(check(profile) for profile in profiles))
        available = [
            profile
            for profile in self.engines
            if profile.healthy
            and profile.engine_id not in self._unavailable_engine_ids
        ]
        if profiles and not available:
            self._stop_reason = "NO_HEALTHY_WORKER_PROFILE"
            self._stop_requested = True
            self.graph.add_dead_end(
                actor="coordinator",
                description="NO_HEALTHY_WORKER_PROFILE",
            )

    async def _race(self) -> None:
        self._change_phase(MutekiPhase.RACE)
        profiles = [
            profile
            for profile in self.engines
            if profile.healthy
            and profile.engine_id not in self._unavailable_engine_ids
        ]
        # The native Muteki Worker owns its own Sandbox/solver lifecycle.  The
        # legacy callback path does not: it commonly closes over one
        # ToolGateway/SQLAlchemy session.  Running several Race callbacks at
        # once against that shared session causes ``Session is already
        # flushing`` and loses the Race handoff before the Coordinator can
        # read the Blackboard.  Keep the compatibility path to one bounded
        # scout; native external workers retain the official multi-engine Race
        # semantics because their adapter boundary is explicit.
        if self.pool.external_runner is None and self.pool.engine_pool is None:
            profiles = profiles[:1]
        # Authentication and quota have already been exercised by the
        # production Sandbox health gate in ``_prepare``.  A failed CLI profile
        # must never reach this dispatch point, otherwise the whole Race window
        # is spent waiting for an engine that cannot complete a turn.
        if len(profiles) <= 1:
            # Preserve the established single-Codex/single-Worker behavior.
            # Route specialization is only needed when several engines would
            # otherwise perform the same whole-challenge reconnaissance.
            for profile in profiles:
                await self._spawn(profile, role="race")
        else:
            for index, profile in enumerate(profiles):
                lane = _RACE_LANES[index % len(_RACE_LANES)]
                cycle = index // len(_RACE_LANES) + 1
                lane_key = lane["lane_key"] if cycle == 1 else f"{lane['lane_key']}:{cycle}"
                route_hash = f"race:{lane_key}"
                goal = f"Race reconnaissance lane: {lane['label']}. Do not duplicate other lanes."
                await self._spawn(
                    profile,
                    role="race",
                    goal=goal,
                    payload={
                        "route_hash": route_hash,
                        "branch_id": f"branch:{lane_key}",
                        "lane_key": lane_key,
                        "race_lane": lane_key,
                    },
                )
        if not self.pool.active_count:
            return
        try:
            await asyncio.wait_for(
                self.pool.wait(),
                timeout=max(
                    1.0,
                    float(self.config.worker_timeout_seconds)
                    + max(0.0, float(self.config.worker_wait_grace_seconds)),
                ),
            )
        except asyncio.TimeoutError:
            # Official Swarm treats Race as a bounded scout.  A timed-out scout
            # must not discard facts already written to the shared Blackboard;
            # recover the Worker and continue through the Coordinator slow path.
            self.graph.add_dead_end(actor="coordinator", description="RACE_WORKER_TIMEOUT")
            await self._recover_timed_out_workers()
        self._release_completed_semantics()
        self._schedule_health_retry()

    async def _recover_timed_out_workers(self) -> None:
        """Reclaim a timed-out Worker without terminating the solve.

        This mirrors the upstream Swarm boundary: one Worker turn may be
        abandoned, while the run-scoped Sandbox, Graph and Evidence remain
        available to the next Reason/Worker cycle.
        """

        active_jobs = tuple(self.pool.active_jobs())
        overdue_keys_reader = getattr(self.official_worker, "overdue_job_keys", None)
        overdue_keys = (
            set(overdue_keys_reader())
            if callable(overdue_keys_reader)
            else {
                "|".join((str(job.worker_id or ""), str(job.intent_id or "")))
                for job in active_jobs
            }
        )
        timed_out_worker_ids: set[str] = set()
        if self.official_worker is not None:
            cancel_active = getattr(self.official_worker, "cancel_active", None)
            if callable(cancel_active):
                for job in active_jobs:
                    job_key = "|".join(
                        (
                            str(job.worker_id or ""),
                            str(job.intent_id or ""),
                        )
                    )
                    if job_key not in overdue_keys:
                        continue
                    await cancel_active(job_key=job_key)
                    timed_out_worker_ids.add(str(job.worker_id))
        if self._health_retry_task is not None and not self._health_retry_task.done():
            self._health_retry_task.cancel()
            await asyncio.gather(self._health_retry_task, return_exceptions=True)
        await self.pool.cancel_workers(timed_out_worker_ids)
        self._release_completed_semantics()

    async def _dispatch_open_intent(self) -> None:
        capacity = min(self.pool.max_workers, self.stage_policy.get_max_workers(self.phase))
        while self.pool.active_count < capacity:
            selector = getattr(self.graph, "dispatchable_intents", None)
            intents = list(selector()) if callable(selector) else list(self.graph.intents(status="open"))
            if not intents:
                return
            profile = self._next_available_profile()
            if profile is None:
                return
            item = self._select_route_intent(intents)
            if item is None:
                return
            if not await self._spawn(profile, role="explore", intent_id=item.intent_id, goal=item.description, payload=item.payload or {}):
                return
            self._dispatched_intents.add(item.intent_id)

    async def _dispatch_review(self, *, goal: str = "review shared blackboard") -> None:
        if not self.stage_policy.can_spawn(self.phase, "review"):
            return
        if self.pool.active_count >= min(self.pool.max_workers, self.stage_policy.get_max_workers(self.phase)):
            return
        profile = self._next_available_profile()
        if profile is not None:
            await self._spawn(profile, role="review", goal=goal)

    async def _dispatch_bootstrap(self) -> None:
        """Run one bounded official whole-challenge recovery worker.

        This mirrors Muteki's re-bootstrap path after a barren Reason cycle:
        the worker receives the existing Blackboard as a head-start and the
        official CLI/Sandbox owns the next exploratory turn.  It is available
        only for the explicit upstream worker backends; the normal callback
        path remains Intent-bound and unchanged.
        """

        if self.config.worker_backend not in {"upstream_local", "upstream_container"}:
            return
        limit = max(0, int(self.config.max_bootstrap_workers))
        if limit > 0 and self._bootstrap_workers >= limit:
            return
        capacity = min(self.pool.max_workers, self.stage_policy.get_max_workers(self.phase))
        if self.pool.active_count >= capacity:
            return
        profile = self._next_available_profile()
        if profile is None:
            return
        goal = (
            "The current evidence-backed route produced no verified finding. "
            "Treat the existing SharedGraph as a head-start, avoid concluded "
            "directions, and pursue a materially different bounded route toward "
            "the challenge goal. Continue only with real evidence and stop if "
            "the official provenance gate has no supported result."
        )
        if await self._spawn(profile, role="bootstrap", goal=goal):
            self._bootstrap_workers += 1
            self.graph.emit_event(
                actor="coordinator",
                event_type=EventType.COORDINATOR_DIRECTIVE,
                payload={
                    "directive": "rebootstrap",
                    "reason_code": "REASON_ROUTE_EXHAUSTED",
                    "worker_role": "bootstrap",
                    "attempt": self._bootstrap_workers,
                },
            )

    async def _spawn(self, profile: EngineProfile, *, role: str, intent_id: str | None = None, goal: str = "", payload: dict | None = None) -> bool:
        if not self.stage_policy.can_spawn(self.phase, role):
            return False
        if self.pool.active_count >= min(self.pool.max_workers, self.stage_policy.get_max_workers(self.phase)):
            return False
        worker_budget = self.config.max_total_workers
        if worker_budget is not None and self._spawned_workers >= max(1, int(worker_budget)):
            if not self._budget_exhausted:
                self._budget_exhausted = True
                self._stop_reason = "WORKER_BUDGET_EXHAUSTED"
                self.graph.add_dead_end(
                    actor="coordinator",
                    description="WORKER_BUDGET_EXHAUSTED",
                )
            return False
        self._worker_number += 1
        worker_id = f"worker-{self._worker_number}"
        resolved_payload = dict(payload or {})
        reservation = None
        if intent_id:
            item = next((candidate for candidate in self.graph.intents() if candidate.intent_id == intent_id), None)
            if item is not None:
                resolved_payload = {**dict(item.payload or {}), **resolved_payload}
            resolved_payload["route_hash"] = stable_route_hash(resolved_payload, goal=goal)
            resolved_payload["branch_id"] = stable_branch_id(
                resolved_payload["route_hash"],
                str(resolved_payload.get("branch_id") or ""),
            )
            decision = acquire_dispatch_semantics(
                self.graph,
                worker_id=worker_id,
                intent_id=intent_id,
                payload=resolved_payload,
            )
            if not decision.allowed:
                self.graph.release_intent(worker=worker_id, intent_id=intent_id)
                self._worker_number -= 1
                return False
            reservation = decision.reservation
            claim_lease_seconds = max(
                300.0,
                float(self.config.worker_timeout_seconds)
                + max(0.0, float(self.config.worker_wait_grace_seconds))
                + 300.0,
            )
            if not self.graph.claim_intent(
                worker=worker_id,
                intent_id=intent_id,
                lease_s=claim_lease_seconds,
            ):
                if reservation is not None:
                    release_dispatch_semantics(self.graph, reservation)
                self._worker_number -= 1
                return False
        elif resolved_payload:
            resolved_payload["route_hash"] = stable_route_hash(resolved_payload, goal=goal)
            resolved_payload["branch_id"] = stable_branch_id(
                resolved_payload["route_hash"],
                str(resolved_payload.get("branch_id") or ""),
            )
            decision = acquire_dispatch_semantics(
                self.graph,
                worker_id=worker_id,
                intent_id=intent_id or f"{role}:{worker_id}",
                payload=resolved_payload,
            )
            if not decision.allowed:
                self._worker_number -= 1
                return False
            reservation = decision.reservation
        route_hash = str(resolved_payload.get("route_hash") or stable_route_hash(resolved_payload, goal=goal))
        branch_id = str(resolved_payload.get("branch_id") or stable_branch_id(route_hash))
        if route_hash in self._active_route_hashes:
            self._worker_number -= 1
            if intent_id:
                self.graph.release_intent(worker=worker_id, intent_id=intent_id)
            if reservation is not None:
                release_dispatch_semantics(self.graph, reservation)
            return False
        engine_attempt_id = f"{self.graph.challenge_id}:{profile.engine_id}:{worker_id}:{route_hash}"
        resolved_payload["route_hash"] = route_hash
        resolved_payload["branch_id"] = branch_id
        resolved_payload["engine_attempt_id"] = engine_attempt_id
        record_attempt = getattr(self.graph, "record_engine_attempt", None)
        if intent_id and callable(record_attempt):
            record_attempt(
                intent_id=intent_id,
                engine_attempt_id=engine_attempt_id,
                actor="coordinator",
            )
        job = WorkerJob(
            worker_id=worker_id,
            role=role,
            engine_id=profile.engine_id,
            graph_path=str(self.graph.db_path),
            challenge_id=self.graph.challenge_id,
            intent_id=intent_id,
            goal=goal,
            payload=resolved_payload,
            driver_profile=dict(profile.driver_profile or {}),
            route_hash=route_hash,
            branch_id=branch_id,
            engine_attempt_id=engine_attempt_id,
            environment={
                "MUTEKI_BLACKBOARD_DB": str(self.graph.db_path),
                "MUTEKI_WORKER_ID": worker_id,
                "MUTEKI_INTENT_ID": str(intent_id or ""),
                "MUTEKI_CHALLENGE_ID": self.graph.challenge_id,
                "MUTEKI_WORKSPACE": str(self.graph.db_path.parent.parent),
                **dict(profile.environment),
            },
        )
        spawned = await self.pool.spawn(job)
        if spawned:
            self._spawned_workers += 1
            self._active_route_hashes.add(route_hash)
        if not spawned:
            if intent_id:
                self.graph.release_intent(worker=worker_id, intent_id=intent_id)
            if reservation is not None:
                release_dispatch_semantics(self.graph, reservation)
        elif spawned and reservation is not None:
            self._semantic_reservations[worker_id] = reservation
        return spawned

    def _next_available_profile(self) -> EngineProfile | None:
        """Choose the next healthy engine using a fair rotating cursor."""

        candidates = [
            profile
            for profile in self.engines
            if profile.healthy
            and profile.engine_id not in self._unavailable_engine_ids
            and profile.engine_id not in self.pool.active_engine_ids
        ]
        if not candidates:
            return None
        # Preserve configured engine order while rotating the starting point.
        for offset in range(len(self.engines)):
            index = (self._engine_cursor + offset) % len(self.engines)
            profile = self.engines[index]
            if profile in candidates:
                self._engine_cursor = (index + 1) % len(self.engines)
                return profile
        return candidates[0]

    def _select_route_intent(self, intents):
        """Select one new route, or an explicitly review-approved retry."""

        all_items = list(self.graph.intents())
        completed_routes = {
            self._intent_route(item)
            for item in all_items
            if str(item.status).casefold() == "done" and self._intent_route(item)
        }
        active_routes = {
            reservation.route_hash
            for reservation in self._semantic_reservations.values()
            if reservation.route_hash
        }
        active_routes.update(self._active_route_hashes)
        seen: set[str] = set()
        for item in intents:
            route = self._intent_route(item)
            if route and route in active_routes:
                continue
            if route and route in seen:
                continue
            if route and route in completed_routes and not self._review_approved_retry(item.payload):
                continue
            if route:
                seen.add(route)
            return item
        return None

    @staticmethod
    def _intent_route(item) -> str:
        return stable_route_hash(
            dict(item.payload or {}),
            intent_id=str(item.intent_id or ""),
            goal=str(item.description or ""),
        )

    @staticmethod
    def _review_approved_retry(payload: Mapping[str, object] | None) -> bool:
        values = dict(payload or {})
        if any(bool(values.get(key)) for key in ("review_approved", "review_approved_retry")):
            return True
        if values.get("retry_of"):
            return True
        rationale = str(values.get("rationale") or "").casefold()
        return "route reopened by review" in rationale

    def _release_completed_semantics(self) -> None:
        """Release one-shot worker locks after the pool's wait barrier."""

        if self.pool.active_count:
            return
        for reservation in tuple(self._semantic_reservations.values()):
            release_dispatch_semantics(self.graph, reservation)
        self._semantic_reservations.clear()
        self._active_route_hashes.clear()

    async def _review_runner(self, job: WorkerJob) -> WorkerOutcome:
        result = ReviewWorker(self.graph, worker_id=job.worker_id).run()
        apply_review_proposals = getattr(self.graph, "apply_review_proposals", None)
        decisions = apply_review_proposals(actor="coordinator") if callable(apply_review_proposals) else []
        accepted = sum(1 for item in decisions if item.get("decision") == "accepted")
        deferred = sum(1 for item in decisions if item.get("decision") == "deferred")
        return WorkerOutcome(
            job.worker_id,
            "COMPLETED",
            result=(
                f"facts={len(result.suspicious_fact_ids)};"
                f"branches={len(result.branch_intent_ids)};"
                f"review_accepted={accepted};review_deferred={deferred}"
            ),
        )

    async def _external_worker_runner(self, job: WorkerJob) -> WorkerOutcome:
        workspace = str(job.environment.get("MUTEKI_WORKSPACE") or self.graph.db_path.parent.parent)
        if not job.intent_id:
            return WorkerOutcome(job.worker_id, "FAILED", result="EXTERNAL_WORKER_REQUIRES_INTENT")
        if self.config.worker_backend == "cli" and self.cli_driver is not None:
            result = await self.cli_driver.run_worker(
                intent_id=job.intent_id,
                engine=job.engine_id,
                workspace=workspace,
                blackboard=str(self.graph.db_path),
                worker_id=job.worker_id,
                timeout_seconds=self.config.worker_timeout_seconds,
            )
        elif self.config.worker_backend == "container" and self.container_executor is not None:
            result = await self.container_executor.run_worker_async(
                job.intent_id,
                workspace,
                str(self.graph.db_path),
                engine=job.engine_id,
                timeout=self.config.worker_timeout_seconds,
                worker_id=job.worker_id,
                challenge_id=self.graph.challenge_id,
            )
        else:
            return WorkerOutcome(job.worker_id, "FAILED", result="EXTERNAL_WORKER_NOT_CONFIGURED")
        status = "COMPLETED" if result.returncode == 0 else "FAILED"
        detail = result.stderr or result.stdout
        return WorkerOutcome(
            job.worker_id,
            status,
            result=detail[-2000:],
            result_code=(
                WorkerResultCode.COMPLETED.value
                if status == "COMPLETED"
                else WorkerResultCode.WORKER_FAILURE.value
            ),
        )

    async def _official_worker_runner(self, job: WorkerJob) -> WorkerOutcome:
        if self.official_worker is None:
            return WorkerOutcome(
                job.worker_id,
                "FAILED",
                result="OFFICIAL_WORKER_NOT_CONFIGURED",
                result_code=WorkerResultCode.EXTERNAL_BLOCKER.value,
                failure_reason="OFFICIAL_WORKER_NOT_CONFIGURED",
            )
        native_cursor = self._native_event_cursor()
        result: OfficialWorkerResult = await self.official_worker.execute(job)
        usage_bridge = getattr(self, "official_worker_usage_bridge", None)
        if usage_bridge is not None:
            try:
                usage_result = usage_bridge(job, result)
                if inspect.isawaitable(usage_result):
                    await usage_result
            except Exception:
                # Accounting must remain observability-only.  A failed metrics
                # write cannot turn a bounded Worker result into a solver
                # execution failure.
                self.graph.emit_event(
                    actor=job.worker_id,
                    event_type="worker.usage.record_failed",
                    payload={"reason_code": "OFFICIAL_WORKER_USAGE_RECORD_FAILED"},
                )
        dead_end = self._native_dead_end_signal(job, native_cursor)
        if dead_end is None:
            candidate_signal = getattr(result, "dead_end_signal", None)
            if candidate_signal is not None:
                dead_end = candidate_signal
        if dead_end is None and str((result.metadata or {}).get("result_code") or "").upper() == WorkerResultCode.ROUTE_EXHAUSTED.value:
            from .outcomes import DeadEndKind, DeadEndSignal

            dead_end = DeadEndSignal(
                kind=DeadEndKind.ROUTE_EXHAUSTED,
                reason="No new endpoint remains on the assigned route.",
                route_hash=stable_route_hash(
                    job.payload,
                    intent_id=str(job.intent_id or ""),
                    goal=job.goal,
                ),
                evidence_refs=tuple(getattr(result, "evidence_refs", ()) or ()),
            )
        if dead_end is None:
            dead_end = signal_from_worker_text(
                getattr(result, "output", ""),
                payload=getattr(job, "payload", {}) or {},
                intent_id=str(job.intent_id or ""),
                goal=str(getattr(job, "goal", "") or ""),
                evidence_refs=tuple(getattr(result, "evidence_refs", ()) or ()),
            )
        if dead_end is not None:
            # Native CliSolver has already appended this event to the official
            # SharedGraph.  The pool consumes the signal to keep intent/result
            # semantics uniform, while avoiding a second Graph dead-end.
            if not dead_end.already_persisted:
                self._conclude_official_intent(job, dead_end.kind.value)
            return WorkerOutcome(
                job.worker_id,
                "COMPLETED",
                result=dead_end.reason,
                result_code=dead_end.kind.value.upper(),
                dead_end=dead_end,
            )
        if result.success and not result.evidence_refs:
            # Native execution is never authoritative by itself.  Without an
            # injected bridge to the existing Evidence authority, fail closed
            # instead of allowing a successful CLI result to advance the graph.
            emit_event = getattr(self.graph, "emit_event", None)
            if callable(emit_event):
                emit_event(
                    actor=job.worker_id,
                    event_type="worker.blocked",
                    payload={"reason_code": "EVIDENCE_BRIDGE_REQUIRED"},
                )
            return WorkerOutcome(
                job.worker_id,
                "FAILED",
                result="EVIDENCE_BRIDGE_REQUIRED",
                result_code=WorkerResultCode.EXTERNAL_BLOCKER.value,
                failure_reason="EVIDENCE_BRIDGE_REQUIRED",
            )
        if result.status not in {"COMPLETED"}:
            result_code = failure_code(result.status, result.metadata)
            failure_reason = _safe_worker_reason(
                (result.metadata or {}).get("reason")
                if isinstance(result.metadata, Mapping)
                else ""
            ) or _safe_worker_reason(getattr(result, "status", ""))
            self._conclude_official_intent(job, result_code.value)
            return WorkerOutcome(
                job.worker_id,
                "FAILED",
                result=result.status,
                result_code=result_code.value,
                failure_reason=failure_reason,
            )
        verified_flag = ""
        if result.success and result.evidence_refs:
            self._record_official_evidence(job, result, native_cursor=native_cursor)
            verified_flag = ""
            metadata = result.metadata if isinstance(result.metadata, dict) else {}
            candidate = str(metadata.get("verified_flag") or "").strip()
            if candidate:
                flag_found = getattr(self.graph, "flag_found", None)
                if callable(flag_found):
                    flag_found(
                        actor=job.worker_id,
                        flag=candidate,
                        artifact_id=str(result.evidence_refs[0]),
                        intent_id=job.intent_id,
                    )
                    verified_flag = candidate
            self._conclude_official_intent(
                job,
                "FLAG_VERIFIED" if verified_flag else "EVIDENCE_RECORDED",
            )
        elif not result.success:
            result_code = failure_code(result.status, result.metadata)
            self._conclude_official_intent(job, result_code.value)
            return WorkerOutcome(
                job.worker_id,
                "FAILED",
                result=result.status,
                result_code=result_code.value,
                failure_reason=_safe_worker_reason(
                    (result.metadata or {}).get("reason")
                    if isinstance(result.metadata, Mapping)
                    else ""
                ),
            )
        # The native worker is deliberately not promoted to a solved finding here.
        # It must write evidence-backed facts through the graph/Evidence adapter.
        return WorkerOutcome(
            job.worker_id,
            "COMPLETED" if result.success else "FAILED",
            flag_found=bool(verified_flag),
            result=result.status,
            result_code=WorkerResultCode.COMPLETED.value if result.success else WorkerResultCode.WORKER_FAILURE.value,
        )

    def _native_dead_end_signal(self, job: WorkerJob, cursor: int | None):
        """Read explicit dead-end markers emitted by the native SharedGraph."""

        if cursor is None:
            return None
        reader = getattr(self.graph, "native_dead_end_events_since", None)
        if not callable(reader):
            return None
        try:
            events = reader(cursor)
        except (OSError, RuntimeError, TypeError, ValueError):
            return None
        for event in events:
            event_actor = str(event.get("actor") or "") if isinstance(event, Mapping) else ""
            if event_actor and event_actor not in {
                str(job.worker_id),
                str(job.engine_id),
            }:
                # Race Workers share the native graph.  Do not attribute a
                # different Worker's explicit route ruling to this job.
                continue
            payload = event.get("payload") if isinstance(event, Mapping) else {}
            reason = payload.get("reason") if isinstance(payload, Mapping) else ""
            signal = signal_from_worker_text(
                f"DEADEND={reason}",
                payload=job.payload,
                intent_id=str(job.intent_id or ""),
                goal=job.goal,
                already_persisted=True,
            )
            if signal is not None:
                return signal
        return None

    def _reason_provider_telemetry(self) -> dict[str, object]:
        """Expose Reason selection/fallback state without model content."""

        provider = getattr(self.reason, "provider", None)
        if provider is None:
            return {
                "provider": "none",
                "provider_kind": "deterministic_fallback",
            }
        return {
            "provider": type(provider).__name__[:100],
            "provider_kind": (
                "coordinator_model"
                if hasattr(provider, "last_error_code")
                else "deterministic_strategy"
            ),
            "provider_error_code": str(getattr(provider, "last_error_code", "") or "")[:80],
        }

    def _native_event_cursor(self) -> int | None:
        cursor = getattr(self.graph, "native_event_cursor", None)
        if not callable(cursor):
            return None
        try:
            return int(cursor())
        except (TypeError, ValueError, OSError):
            return None

    def _native_observations_since(
        self,
        job: WorkerJob,
        cursor: int | None,
    ) -> list[SafeNativeObservation]:
        if cursor is None:
            return []
        read_events = getattr(self.graph, "native_fact_events_since", None)
        if not callable(read_events):
            return []
        try:
            events = read_events(cursor)
        except (OSError, RuntimeError, TypeError, ValueError):
            return []
        payload = dict(getattr(job, "payload", {}) or {})
        expected_action = str(payload.get("tool_name") or payload.get("tool") or "")
        return extract_native_observations(
            events,
            intent_id=str(job.intent_id or ""),
            expected_action=expected_action,
        )

    def _record_official_evidence(
        self,
        job: WorkerJob,
        result: OfficialWorkerResult,
        *,
        native_cursor: int | None = None,
    ) -> None:
        """Persist only a safe observation summary and durable Evidence refs.

        Native Worker output is intentionally excluded.  First consume the
        official Blackboard's structured fact product.  The injected bridge is
        the authority for Evidence references; the projection makes the safe
        fields visible to the next Coordinator/Reason turn and links them to
        the claimed official intent.  A generic fact remains the fail-closed
        fallback when the Worker did not produce a structured product.
        """

        refs = tuple(str(ref) for ref in result.evidence_refs if str(ref))
        if not refs:
            return
        observations = self._native_observations_since(job, native_cursor)
        if observations:
            for observation in observations:
                self._record_native_observation(job, observation, refs)
            return
        add_fact = getattr(self.graph, "add_fact", None)
        if not callable(add_fact):
            return
        intent_id = str(job.intent_id or job.worker_id)
        attempt_feedback = _safe_unverified_attempt(job)
        if attempt_feedback is not None:
            add_fact(
                actor=job.worker_id,
                content=json.dumps(
                    attempt_feedback,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                verified=False,
                evidence_refs=list(refs),
                dedupe_key=(
                    f"muteki-native-attempt:{intent_id}:"
                    f"{attempt_feedback['tool']}"
                ),
            )
            return
        add_fact(
            actor=job.worker_id,
            content="Native Worker completed a bounded evidence-backed observation",
            verified=False,
            evidence_refs=list(refs),
            dedupe_key=f"muteki-native-worker:{intent_id}:{','.join(refs)}",
        )

    def _record_native_observation(
        self,
        job: WorkerJob,
        observation: SafeNativeObservation,
        refs: tuple[str, ...],
    ) -> None:
        content = json.dumps(
            observation.payload(evidence_refs=refs),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        add_native_fact = getattr(self.graph, "add_native_fact", None)
        if callable(add_native_fact):
            add_native_fact(
                actor=job.worker_id,
                content=content,
                evidence_refs=list(refs),
                intent_id=job.intent_id,
            )
            return
        add_fact = getattr(self.graph, "add_fact", None)
        if callable(add_fact):
            add_fact(
                actor=job.worker_id,
                content=content,
                verified=False,
                evidence_refs=list(refs),
                dedupe_key=(
                    f"muteki-native-observation:{job.intent_id or job.worker_id}:"
                    f"{observation.fact_sequence}"
                ),
            )

    def _conclude_official_intent(self, job: WorkerJob, result: str) -> None:
        """Close a native intent without promoting its observation to a finding."""

        if not job.intent_id:
            return
        conclude_intent = getattr(self.graph, "conclude_intent", None)
        if callable(conclude_intent):
            conclude_intent(actor=job.worker_id, intent_id=job.intent_id, result=str(result))

    def _record_worker_step(self, payload: dict[str, str]) -> None:
        worker_id = str(payload.get("worker_id") or "official-worker")
        self.graph.emit_event(
            actor=worker_id,
            event_type=EventType.WORKER_STEP,
            payload={
                "worker_id": worker_id,
                "kind": str(payload.get("kind") or ""),
                "tool": str(payload.get("tool") or ""),
                "session": str(payload.get("session") or ""),
            },
        )

    def request_stop(self, reason: str = "STOP_REQUESTED") -> None:
        self._stop_requested = True
        self._stop_reason = str(reason or "STOP_REQUESTED")[:120]

    async def send_control(self, worker_id: str, command: str, payload: dict | None = None) -> None:
        """Forward an operator command when a control transport is configured."""

        if self.control_receiver is None:
            raise RuntimeError("Muteki control receiver is not configured")
        await self.control_receiver.send_command(worker_id, command, payload)

    async def broadcast_control(self, command: str, payload: dict | None = None) -> None:
        if self.control_receiver is None:
            raise RuntimeError("Muteki control receiver is not configured")
        await self.control_receiver.broadcast(command, payload)

    async def finalize(self, *, reason: str) -> None:
        if self._finalized:
            return
        self._finalized = True
        self._change_phase(MutekiPhase.FINALIZE)
        # Cancel the native official Worker first.  Cancelling the pool task
        # first only cancels the asyncio wrapper; a CLI/RCP process running in
        # ``to_thread`` can then outlive the run and write late Blackboard
        # facts.  Official Muteki reaps the execution window before retiring
        # its task; keep that ordering at the integration boundary too.
        if self.official_worker is not None:
            cancel_active = getattr(self.official_worker, "cancel_active", None)
            if callable(cancel_active):
                await cancel_active()
        for task in (self._health_retry_task, self._reason_health_retry_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (self._health_retry_task, self._reason_health_retry_task) if task is not None),
            return_exceptions=True,
        )
        await self.pool.cancel_all()
        self._release_completed_semantics()
        if self.official_worker is not None:
            await self.official_worker.stop()
        self.graph.release_claims(actor="coordinator")
        final_reason = self._stop_reason or reason
        self.graph.emit_event(actor="coordinator", event_type=EventType.RUN_FINISHED, payload={"reason": final_reason})

    def _change_phase(self, phase: MutekiPhase) -> None:
        if self.phase is phase:
            return
        if not self.stage_policy.can_transition(self.phase, phase):
            raise ValueError(f"illegal Muteki stage transition: {self.phase} -> {phase}")
        self.phase = phase
        self.graph.emit_event(actor="coordinator", event_type=EventType.PHASE_CHANGED, payload={"phase": phase.value})


def _safe_worker_reason(value: object) -> str:
    """Return only an adapter status code for durable dead-end telemetry."""

    text = str(value or "").strip()
    if len(text) > 120 or not re.fullmatch(r"[A-Z0-9][A-Z0-9_.:-]*", text):
        return ""
    return text

def _safe_unverified_attempt(job: WorkerJob) -> dict[str, str] | None:
    """Return safe action identity when the Worker produced no typed result.

    This is execution feedback, not a vulnerability fact. It lets Strategy
    retire the exact attempted field/metadata route without treating the lack
    of a typed observation as a verified negative result.
    """

    payload = dict(getattr(job, "payload", {}) or {})
    tool = str(payload.get("tool_name") or payload.get("tool") or "").strip()
    if not tool or not all(
        char in "abcdefghijklmnopqrstuvwxyz0123456789_" for char in tool
    ):
        return None
    feedback: dict[str, str] = {"tool": tool, "observation_status": "UNAVAILABLE"}
    arguments = payload.get("arguments")
    if not isinstance(arguments, dict):
        return feedback
    if tool == "sql_boolean_compare":
        field = str(arguments.get("test_field") or "").strip()
        if field and all(
            char in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-"
            for char in field[:80]
        ):
            feedback["test_field"] = field[:80]
    elif tool == "mysql_metadata_discovery":
        stage = str(arguments.get("stage") or "").strip().lower()
        expression = str(arguments.get("target_expression") or "").strip()
        if stage in {"database", "tables", "columns"}:
            feedback["stage"] = stage
        if expression in {
            "DATABASE()",
            "information_schema.tables",
            "information_schema.columns",
        }:
            feedback["target_expression"] = expression
    return feedback


__all__ = ["CoordinatorConfig", "MutekiCoordinator"]
