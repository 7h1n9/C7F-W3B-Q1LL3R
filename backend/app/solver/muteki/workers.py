from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .events import EventType
from .graph import Intent, MutekiGraph
from .outcomes import (
    DeadEndKind,
    DeadEndSignal,
    WorkerResultCode,
    route_key,
    signal_from_worker_text,
)


@dataclass(frozen=True, slots=True)
class EngineProfile:
    engine_id: str
    command: tuple[str, ...] = ()
    healthy: bool = True
    worker_class: str = "code"
    environment: Mapping[str, str] = field(default_factory=dict)
    # Optional official Muteki driver profile.  A bare engine id is still the
    # default for legacy callers; custom endpoints must travel with the job so
    # the Worker, not the Coordinator, owns endpoint resolution.
    driver_profile: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkerJob:
    worker_id: str
    role: str
    engine_id: str
    graph_path: str
    challenge_id: str
    intent_id: str | None = None
    goal: str = ""
    environment: Mapping[str, str] = field(default_factory=dict)
    payload: Mapping[str, object] = field(default_factory=dict)
    driver_profile: Mapping[str, Any] = field(default_factory=dict)
    route_hash: str = ""
    branch_id: str = ""
    engine_attempt_id: str = ""


@dataclass(frozen=True, slots=True)
class WorkerOutcome:
    worker_id: str
    status: str
    flag_found: bool = False
    result: str = ""
    result_code: str = WorkerResultCode.COMPLETED.value
    dead_end: DeadEndSignal | None = None
    failure_reason: str = ""


WorkerRunner = Callable[[WorkerJob], Awaitable[WorkerOutcome]]
ReviewHandler = Callable[[WorkerJob], Awaitable[WorkerOutcome]]


class MutekiWorkerPool:
    """One-shot worker tasks with explicit capacity and cancellation."""

    def __init__(self, graph: MutekiGraph, runner: WorkerRunner, *, max_workers: int = 10, review_handler: ReviewHandler | None = None, engine_pool: Any | None = None, external_runner: WorkerRunner | None = None, external_runner_exclusive: bool = False) -> None:
        self.graph = graph
        self.runner = runner
        self.max_workers = max(1, max_workers)
        self.review_handler = review_handler
        self.engine_pool = engine_pool
        self.external_runner = external_runner
        # Native Muteki production sets this when the official Sandbox owns
        # every execution-capable Worker.  In that mode no local model or
        # compatibility callback may fall through to ToolGateway/Kali Runner.
        self.external_runner_exclusive = bool(external_runner_exclusive)
        self._tasks: dict[str, asyncio.Task[WorkerOutcome]] = {}
        self._jobs: dict[str, WorkerJob] = {}

    @property
    def active_count(self) -> int:
        return sum(not task.done() for task in self._tasks.values())

    @property
    def active_engine_ids(self) -> frozenset[str]:
        return frozenset(job.engine_id for worker_id, job in self._jobs.items() if not self._tasks[worker_id].done())

    def active_jobs(self) -> tuple[WorkerJob, ...]:
        """Return the current Worker jobs without exposing asyncio Tasks."""

        return tuple(
            job
            for worker_id, job in self._jobs.items()
            if worker_id in self._tasks and not self._tasks[worker_id].done()
        )

    def get_available_engine(self, preferred: str | None = None) -> str | None:
        candidates = ([preferred] if preferred else []) + [job.engine_id for job in self._jobs.values() if job.engine_id not in {preferred}]
        for candidate in candidates:
            if candidate and candidate not in self.active_engine_ids:
                return candidate
        return None

    async def spawn(self, job: WorkerJob) -> bool:
        if self.active_count >= self.max_workers or job.worker_id in self._tasks:
            return False
        self.graph.emit_event(actor=job.worker_id, event_type=EventType.WORKER_STARTED, payload={"role": job.role, "engine_id": job.engine_id, "intent_id": job.intent_id, "route_hash": job.route_hash, "branch_id": job.branch_id, "engine_attempt_id": job.engine_attempt_id})
        task = asyncio.create_task(self._run(job), name=f"muteki-worker-{job.worker_id}")
        self._tasks[job.worker_id] = task
        self._jobs[job.worker_id] = job
        return True

    async def _run(self, job: WorkerJob) -> WorkerOutcome:
        try:
            if job.role == "review" and self.review_handler is not None:
                outcome = await self.review_handler(job)
                outcome = self._normalize_outcome(job, outcome)
                self._apply_outcome(job, outcome)
                return outcome
            # The official Muteki Coordinator also launches bounded bootstrap
            # workers without a claimable Intent.  Keep ordinary callbacks
            # Intent-bound, but let an explicitly configured external backend
            # own the bootstrap lifecycle as well.
            if self.external_runner is not None and (
                # Official Muteki owns both the initial Race scout and
                # Intent/bootstrap turns.  Routing Race through the injected
                # native Worker is essential: otherwise a container-selected
                # run silently falls back to the compatibility ToolGateway for
                # its very first probe, which is exactly the Kali dependency
                # this boundary is meant to remove.
                self.external_runner_exclusive
                or (job.intent_id or job.role in {"bootstrap", "race"})
            ):
                outcome = await self.external_runner(job)
                outcome = self._normalize_outcome(job, outcome)
                self._apply_outcome(job, outcome)
                return outcome
            if self.engine_pool is not None:
                intent = Intent(
                    job.intent_id or f"worker-{job.worker_id}",
                    job.goal or job.role,
                    "open",
                    None,
                    "",
                    payload=dict(job.payload),
                    route_hash=job.route_hash,
                    branch_id=job.branch_id,
                    engine_attempt_id=job.engine_attempt_id,
                )
                workspace = str(job.environment.get("MUTEKI_WORKSPACE") or Path(self.graph.db_path).parent.parent)
                result = await self.engine_pool.execute(intent, workspace, preferred=job.engine_id)
                dead_end = signal_from_worker_text(
                    result.output,
                    payload=job.payload,
                    intent_id=str(job.intent_id or ""),
                    goal=job.goal,
                ) if result.success else None
                outcome = WorkerOutcome(
                    job.worker_id,
                    "COMPLETED" if result.success else "FAILED",
                    result=result.output or str(result.metadata.get("reason") or ""),
                    result_code=(
                        WorkerResultCode.ROUTE_DEAD_END.value
                        if dead_end is not None
                        else (
                            WorkerResultCode.COMPLETED.value
                            if result.success
                            else WorkerResultCode.WORKER_FAILURE.value
                        )
                    ),
                    dead_end=dead_end,
                )
                outcome = self._normalize_outcome(job, outcome)
                self._apply_outcome(job, outcome)
                return outcome
            outcome = await self.runner(job)
            outcome = self._normalize_outcome(job, outcome)
            self._apply_outcome(job, outcome)
            return outcome
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # A Worker/transport failure is execution telemetry, not evidence
            # that the target route is impossible.  Keep the failure visible
            # in the outcome so the Coordinator can recover or retry without
            # poisoning SharedGraph.dead_ends.
            outcome = WorkerOutcome(
                job.worker_id,
                "FAILED",
                result=str(error),
                result_code=WorkerResultCode.WORKER_FAILURE.value,
                failure_reason=type(error).__name__,
            )
            self._apply_outcome(job, outcome)
            return outcome
        finally:
            self.graph.emit_event(
                actor=job.worker_id,
                event_type=EventType.WORKER_FINISHED,
                payload={
                    "role": job.role,
                    "engine_id": job.engine_id,
                    "intent_id": job.intent_id,
                    "route_hash": job.route_hash,
                    "branch_id": job.branch_id,
                    "engine_attempt_id": job.engine_attempt_id,
                },
            )

    def _normalize_outcome(self, job: WorkerJob, outcome: WorkerOutcome | None) -> WorkerOutcome | None:
        """Normalize an explicit marker returned by any Worker implementation."""

        if outcome is None or outcome.dead_end is not None:
            return outcome
        signal = signal_from_worker_text(
            outcome.result,
            payload=job.payload,
            intent_id=str(job.intent_id or ""),
            goal=job.goal,
        )
        if signal is None:
            return outcome
        return replace(
            outcome,
            result_code=WorkerResultCode.ROUTE_DEAD_END.value,
            dead_end=signal,
        )

    def _apply_outcome(self, job: WorkerJob, outcome: WorkerOutcome | None) -> None:
        """Apply only explicit route dead-ends to the shared Graph.

        This is the single compatibility boundary for callback, engine-pool,
        and native Worker outcomes.  Provider failures remain ordinary
        execution results and are never persisted as target dead-ends.
        """

        if outcome is None:
            return
        signal = outcome.dead_end
        if signal is None or signal.kind not in {
            DeadEndKind.ROUTE_DEAD_END,
            DeadEndKind.ROUTE_EXHAUSTED,
        }:
            return
        if not signal.already_persisted:
            route = signal.route_hash or route_key(
                job.payload,
                intent_id=str(job.intent_id or ""),
                goal=job.goal,
            )
            existing = tuple(self.graph.dead_ends())
            marker = f"route={route}"
            if not any(marker in str(item.description) for item in existing):
                evidence = ",".join(signal.evidence_refs[:8])
                description = (
                    f"{signal.kind.value} {marker} "
                    f"engine={job.engine_id[:80]} worker={job.worker_id[:80]}"
                    f" intent={str(job.intent_id or '')[:100]} "
                    f"reason={signal.reason[:240]}"
                )
                if evidence:
                    description += f" evidence_refs={evidence}"
                self.graph.add_dead_end(actor=job.worker_id, description=description)
        if job.intent_id and not signal.already_persisted:
            self.graph.conclude_intent(
                actor=job.worker_id,
                intent_id=job.intent_id,
                result=signal.kind.value,
            )

    async def wait(self) -> tuple[WorkerOutcome | BaseException, ...]:
        tasks = tuple(self._tasks.values())
        if not tasks:
            return ()
        # A Coordinator timeout must not cancel the Worker tasks before the
        # native Worker adapter has received its cancellation signal.  The
        # official Muteki order is cancel native process -> reap execution ->
        # cancel wrapper task.  Shield the barrier so ``asyncio.wait_for``
        # cannot turn a timeout into an untracked background Worker.
        barrier = asyncio.gather(*tasks, return_exceptions=True)
        return tuple(await asyncio.shield(barrier))

    async def cancel_all(self) -> None:
        tasks = tuple(task for task in self._tasks.values() if not task.done())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def cancel_workers(self, worker_ids: set[str] | frozenset[str]) -> None:
        """Cancel only the named compatibility Worker wrappers.

        Native cancellation is performed by the Coordinator's adapter before
        this method.  Keeping the selection at pool level prevents one timed
        out route from cancelling healthy sibling routes.
        """

        tasks = tuple(
            task
            for worker_id, task in self._tasks.items()
            if worker_id in worker_ids and not task.done()
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


__all__ = ["EngineProfile", "MutekiWorkerPool", "WorkerJob", "WorkerOutcome"]
