"""Adapter for the vendored Muteki CLI/Sandbox Worker.

Production Muteki runs use the upstream one-container-per-run Sandbox/Control
Plane.  Legacy ToolGateway/Kali Runner execution is outside this adapter and
is not a fallback after a native Worker failure.  Raw CLI output never becomes
an audit event; callers must turn confirmed output into Evidence before
completion.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import ipaddress
import json
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit, urlunsplit

from muteki.core.wire import normalize_wire_api

from ..adapter.official_evidence import (
    OfficialWorkerEvidenceBridge,
    sanitize_evidence_refs,
)
from ..outcomes import DeadEndSignal
from ..redaction import COMMAND_LIMIT, RESULT_LIMIT, redact

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OfficialWorkerConfig:
    """Execution policy for one optional native Muteki worker backend."""

    backend: str = "local"
    timeout_seconds: int = 900
    # The first CLI turn in a fresh Sandbox may include credential refresh and
    # provider startup.  Keep health bounded, but do not use the old 30-second
    # ContainerResult probe window for the upstream RCP/CliResult contract.
    health_timeout_seconds: int = 120
    # Bounded reap window after the native Muteki cancellation signal.  This
    # covers the adapter's ``to_thread`` future, not another model turn.
    cancel_grace_seconds: float = 15.0
    # Keep the upstream CliSolver turn budget.  The Coordinator still owns
    # Intent scheduling; a single Worker is allowed to complete its bounded
    # assignment over multiple CLI turns instead of being cut off after the
    # first tool call.
    max_turns: int = 80
    web_access: bool = True
    kb_access: bool = False
    image: str | None = None
    network: str = "bridge"
    memory: str | None = None
    cpus: str | None = None
    pids_limit: int | None = None
    protocol: str = "one_shot"
    # Optional host replacement for deployments where the target is published
    # only on the Docker host loopback.  It is deliberately opt-in: many local
    # challenge ranges bind their authorized target address directly on a host
    # interface that is already reachable from the Worker container.  A blind
    # default rewrite to host.docker.internal can turn a reachable target into
    # a connection refusal.
    container_target_host: str | None = None

    def __post_init__(self) -> None:
        if self.backend not in {"local", "container"}:
            raise ValueError(f"unsupported official worker backend: {self.backend}")
        if self.timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")
        if self.health_timeout_seconds < 1:
            raise ValueError("health_timeout_seconds must be positive")
        if self.cancel_grace_seconds <= 0:
            raise ValueError("cancel_grace_seconds must be positive")
        if self.max_turns < 1:
            raise ValueError("max_turns must be positive")
        if self.protocol not in {"one_shot", "cli_solver"}:
            raise ValueError(f"unsupported official worker protocol: {self.protocol}")


@dataclass(frozen=True, slots=True)
class OfficialWorkerResult:
    """Sanitized result metadata returned by the native worker boundary."""

    success: bool
    status: str
    engine: str
    output: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    # Explicit route ruling from a native/function-call Worker.  Execution
    # succeeded, but this particular direction is no longer retryable.
    dead_end_signal: DeadEndSignal | None = None
    # Host-local path to the official Muteki ArtifactStore product.  The path
    # is consumed only by the injected Evidence authority; it is never put in
    # Graph facts, audit payloads, or Worker metadata.
    evidence_artifact_path: str = ""


@dataclass
class _ActiveExecution:
    """Adapter-owned handle for one official Worker execution window.

    Muteki creates an isolated solver object per Worker while the Blackboard and
    Sandbox remain run-scoped.  The adapter therefore needs the same separation:
    cancellation and reaping are keyed by Worker job, never by the adapter as a
    singleton.  ``future`` is the asyncio task awaiting the ``to_thread`` call;
    keeping it here lets timeout recovery wait until the native thread has
    actually returned.
    """

    job: Any
    cancel_event: threading.Event = field(default_factory=threading.Event)
    future: Any | None = None
    native: Any | None = None
    cost_controller: Any | None = None
    started_at: float = field(default_factory=time.monotonic)


class OfficialWorkerAdapter:
    """Run official Muteki Workers inside the selected Sandbox boundary."""

    def __init__(
        self,
        config: OfficialWorkerConfig | None = None,
        *,
        step_callback: Callable[[dict[str, str]], None] | None = None,
        evidence_bridge: OfficialWorkerEvidenceBridge | None = None,
        usage_bridge: Callable[[Any, OfficialWorkerResult], Any] | None = None,
        shared_graph: Any | None = None,
    ) -> None:
        self.config = config or OfficialWorkerConfig()
        self.step_callback = step_callback
        self.evidence_bridge = evidence_bridge
        self.usage_bridge = usage_bridge
        self.shared_graph = shared_graph
        self._container: Any | None = None
        self._run_id: str | None = None
        # Set by ``stop`` and never cleared: a Sandbox that has been torn down
        # must not be resurrected by a late ``start``.  Measured 2026-10-19 --
        # a Run reached COMPLETED_SOLVED at 16:42:22 while its container was
        # (re)started at 16:42:21 and then sat idle with only ``runtime_agent``
        # inside.  The Finalize teardown had already run; something started the
        # Sandbox again afterwards, and nothing was left to tear it down.
        self._stopped = False
        # CLI engines report the command and its output as two separate stream
        # steps (``tool`` then ``tool_result``).  Keying the command by
        # ``call_id`` lets the result step publish both halves together, so one
        # WORKER_COMMAND answers "which command printed this?" without the
        # reviewer having to correlate sequence numbers.
        self._pending_commands: dict[str, str] = {}
        self._account_root: str | None = None
        # ``execute`` runs the official Worker inside ``to_thread``.  Keep one
        # native handle per job so Coordinator cancellation can use Muteki's
        # process/HTTP cancel protocol instead of merely cancelling an asyncio
        # task while the container process continues to run.
        self._active_solver: Any | None = None
        # The CostController belongs to the active native Worker.  Keep it
        # available after cancellation so a reclaimed Worker can still flush
        # usage that the CLI driver parsed before the process was killed.
        self._active_cost_controller: Any | None = None
        self._active_job: Any | None = None
        self._usage_flushed_job_key: str | None = None
        self._usage_flushed_job_keys: set[str] = set()
        self._active_executions: dict[str, _ActiveExecution] = {}
        # This lock is only for cross-thread handle bookkeeping.  It must not
        # serialize Worker execution: official Muteki's Race phase runs one
        # solver per selected engine in parallel.
        self._active_lock = threading.RLock()
        # Shell-emitted VERIFIED_FACT=/DEADEND= markers, buffered per job.
        # ``_on_step`` runs on the CLI's own thread and the board write belongs to
        # the event loop, so the two halves are split: buffer on the step thread,
        # flush onto the loop as soon as a batch is buffered.
        self._tool_markers: dict[str, dict[str, list[str]]] = {}
        # A marker published once stays published: replaying the same
        # ``VERIFIED_FACT=`` line on every later step would fill the actor's
        # candidate bucket with restatements and make ``fact_added`` unreadable.
        self._published_markers: dict[str, set[str]] = {}
        # Incremental publishing needs the loop that owns the board write.  A
        # Worker that only flushed when ``execute`` returned left every finding
        # invisible for the rest of the Run -- measured 2026-10-29: all four
        # facts of a 25-minute Run landed in the same timestamp as
        # ``worker_finished``, so the other lane and every later step of the
        # same lane worked from a board that was still empty.
        self._job_loops: dict[str, Any] = {}
        self._tool_marker_lock = threading.Lock()

    async def start(
        self,
        *,
        run_id: str,
        workspace: str,
        account_root: str | None = None,
    ) -> None:
        """Start the official one-container-per-run Sandbox when requested."""

        if self._stopped:
            # Finalize already tore this Sandbox down.  Provisioning it again
            # would create a container that no owner is left to reap.
            logger.warning(
                "official worker start ignored after teardown run_id=%s", run_id
            )
            return
        self._run_id = str(run_id)
        if self.config.backend != "container":
            self._account_root = account_root
            return
        # The official RCP supervisor connects back through
        # ``host.docker.internal``. A bridge container cannot reach a host
        # listener bound only to 127.0.0.1, so the production container path
        # defaults the receiver to all host interfaces unless the deployment
        # explicitly chose a bind address. The receiver port is not published
        # by the worker container and every link still requires its per-run
        # token handshake.
        os.environ.setdefault("MUTEKI_CONTROL_BIND", "0.0.0.0")
        from muteki.solver.container_exec import ensure_container

        try:
            self._container = await asyncio.to_thread(
                ensure_container,
                str(run_id),
                str(workspace),
                image=self.config.image or os.environ.get(
                    "MUTEKI_WORKER_IMAGE", "ghcr.io/fishcodetech/muteki-worker:latest"
                ),
                network=self.config.network,
                memory=self.config.memory,
                cpus=self.config.cpus,
                pids_limit=self.config.pids_limit,
                account_root=account_root,
            )
        except SystemExit as error:
            # ``SystemExit`` is a ``BaseException``, so every ``except Exception``
            # between here and the supervisor misses it -- including the ones that
            # are supposed to turn a failed Run into FAILED_ENGINE.  Raised from
            # inside the worker thread it therefore travels all the way up the
            # Muteki task, out of the event loop, and terminates the process: one
            # refused filesystem operation during container provisioning took the
            # whole API down, not the Run.
            #
            # Observed for real under a sandbox whose ``shutil.rmtree`` is
            # intercepted and exits on a bulk delete.  Container provisioning is
            # the one step that reaches out to the host filesystem, so it is the
            # right place to make that class of failure an ordinary error.
            raise RuntimeError(
                f"Worker container provisioning aborted (SystemExit {error}); "
                "a host filesystem operation refused to complete."
            ) from error
        if self._stopped:
            # Provisioning can take minutes (image pull, account projection),
            # which is longer than a Run that solves quickly.  Finalize therefore
            # runs its teardown *before* this call returns, and the Sandbox then
            # appears with no owner left to reap it -- measured 2026-10-19: a
            # container started in the same second its Run went
            # COMPLETED_SOLVED and then idled with only ``runtime_agent``
            # inside.  Undo the provisioning here, where the handle still is.
            from muteki.solver.container_exec import teardown_container

            with contextlib.suppress(Exception):
                await asyncio.to_thread(teardown_container, str(run_id), remove=True)
            self._container = None
            return
        # `ensure_container` may replace the operator's account store with a
        # readable per-run projection.  Resolve credential paths against that
        # projection so the container receives only CODEX_HOME/CREDENTIAL_FILE
        # references, never the host secret contents.
        self._account_root = str(
            getattr(self._container, "account_root", None) or account_root or ""
        ) or None
        # The proxy host alias must resolve to IPv4 only before the first engine
        # probe runs; see ``_pin_host_alias_to_ipv4``.
        with contextlib.suppress(Exception):
            await asyncio.to_thread(
                _pin_host_alias_to_ipv4, str(getattr(self._container, "container", "") or "")
            )

    async def stop(self) -> None:
        """Tear down the run Sandbox and clear the local handle."""

        # Before anything else: from here on this instance must not provision
        # a Sandbox again, even if a late caller asks it to.
        self._stopped = True
        await self.cancel_active()
        if self._run_id and self.config.backend == "container":
            from muteki.solver.container_exec import teardown_container

            await asyncio.to_thread(teardown_container, self._run_id)
        self._container = None
        self._run_id = None
        self._account_root = None
        self._active_solver = None
        self._active_cost_controller = None
        self._active_job = None

    async def cancel_active(self, job_key: str | None = None) -> None:
        """Cancel and reap active native Workers without tearing down Sandbox.

        Upstream Swarm reclaims a timed-out Worker and continues the shared
        Blackboard loop.  The run-scoped container must therefore outlive a
        Worker cancellation; full teardown belongs exclusively to ``stop``.

        The important distinction is between cancelling the wrapper task and
        cancelling the native execution.  ``asyncio.to_thread`` cannot kill its
        thread when the wrapper is cancelled, so signal every native handle and
        await the stored execution future for a bounded grace period first.
        """

        with self._active_lock:
            executions = list(self._active_executions.values())
            if job_key is not None:
                executions = [
                    execution
                    for execution in executions
                    if self._job_key(execution.job) == job_key
                ]
            legacy_solver = self._active_solver if not executions else None

        for execution in executions:
            self._cancel_execution(execution)
            await self._flush_active_usage(execution)

        if legacy_solver is not None:
            self._cancel_native(legacy_solver)
            await self._flush_active_usage()
        elif not executions:
            # Keep the diagnostic/test compatibility view working for callers
            # that populate only the legacy usage aliases.  Production
            # executions always use the per-job map above.
            await self._flush_active_usage()

        for execution in executions:
            future = execution.future
            if future is None or future.done():
                await self._flush_active_usage(execution)
                continue
            try:
                await asyncio.wait_for(
                    asyncio.shield(future),
                    timeout=max(0.1, float(self.config.cancel_grace_seconds)),
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                # ``stop`` will tear down the run Sandbox if a broken provider
                # ignores the official cancellation signal.  Keep the handle
                # in the map so a later stop/recovery can retry rather than
                # silently losing the in-flight execution.
                continue
            finally:
                await self._flush_active_usage(execution)

    @staticmethod
    def _cancel_native(native: Any | None) -> None:
        cancel = getattr(native, "cancel", None)
        if not callable(cancel):
            return
        try:
            cancel()
        except Exception:
            pass

    def _cancel_execution(self, execution: _ActiveExecution) -> None:
        execution.cancel_event.set()
        self._cancel_native(execution.native)

    def _execution_for(self, job: Any) -> _ActiveExecution | None:
        with self._active_lock:
            return self._active_executions.get(self._job_key(job))

    def overdue_job_keys(self) -> frozenset[str]:
        """Return only native executions beyond this adapter's Worker bound."""

        now = time.monotonic()
        timeout = float(self.config.timeout_seconds)
        with self._active_lock:
            return frozenset(
                key
                for key, execution in self._active_executions.items()
                if now - execution.started_at >= timeout
            )

    def _cancel_event_for(self, job: Any) -> threading.Event | None:
        execution = self._execution_for(job)
        return execution.cancel_event if execution is not None else None

    def _set_native_handle(
        self,
        job: Any,
        native: Any,
        cost_controller: Any | None = None,
    ) -> None:
        job_key = self._job_key(job)
        with self._active_lock:
            execution = self._active_executions.get(job_key)
            if execution is not None:
                execution.native = native
                if cost_controller is not None:
                    execution.cost_controller = cost_controller
                if execution.cancel_event.is_set():
                    self._cancel_native(native)
            # Keep these aliases for old diagnostics/tests and direct sync
            # helper calls.  They are never used as the authoritative map.
            self._active_solver = native
            self._active_job = job
            if cost_controller is not None:
                self._active_cost_controller = cost_controller

    def _clear_native_handle(self, job: Any, native: Any) -> None:
        job_key = self._job_key(job)
        with self._active_lock:
            execution = self._active_executions.get(job_key)
            if execution is not None and execution.native is native:
                execution.native = None
            if self._active_solver is native:
                self._active_solver = None

    def _drop_execution(self, execution: _ActiveExecution) -> None:
        job_key = self._job_key(execution.job)
        with self._active_lock:
            if self._active_executions.get(job_key) is execution:
                self._active_executions.pop(job_key, None)
            if self._active_job is execution.job:
                self._active_job = None
            if self._active_solver is execution.native:
                self._active_solver = None
            if self._active_cost_controller is execution.cost_controller:
                self._active_cost_controller = None

    async def _flush_active_usage(self, execution: _ActiveExecution | None = None) -> None:
        """Persist a numeric CostController snapshot for a reclaimed Worker.

        This is deliberately limited to token/cost/call counters.  It does not
        inspect CLI output and it never changes the Worker outcome or graph.
        """

        callback = self.usage_bridge
        job = execution.job if execution is not None else self._active_job
        controller = (
            execution.cost_controller
            if execution is not None
            else self._active_cost_controller
        )
        if callback is None or job is None or controller is None:
            return
        job_key = self._job_key(job)
        if job_key in self._usage_flushed_job_keys or self._usage_flushed_job_key == job_key:
            return
        metadata = self._usage_metadata(
            controller,
            engine=self._usage_engine(job),
            role="worker",
        )
        if not any(
            metadata.get(name)
            for name in ("input_tokens", "output_tokens", "cost_usd", "calls")
        ):
            return
        partial = OfficialWorkerResult(
            False,
            "INTERRUPTED",
            str(getattr(job, "engine_id", "") or "codex"),
            metadata={**metadata, "partial": True},
        )
        try:
            persisted = callback(job, partial)
            if inspect.isawaitable(persisted):
                await persisted
        except Exception:
            # Usage is observability-only and must not mask Worker recovery.
            return
        self._usage_flushed_job_key = job_key
        self._usage_flushed_job_keys.add(job_key)

    @staticmethod
    def _job_key(job: Any) -> str:
        return "|".join(
            (
                str(getattr(job, "worker_id", "") or ""),
                str(getattr(job, "intent_id", "") or ""),
            )
        )

    @staticmethod
    def _usage_engine(job: Any) -> str:
        """Return the billable model identity for a reclaimed Worker."""

        profile = getattr(job, "driver_profile", None)
        if isinstance(profile, Mapping):
            if (
                str(profile.get("protocol") or "").strip() == "chat_completions"
                and str(profile.get("model") or "").strip()
            ):
                return str(profile["model"])[:120]
        return str(getattr(job, "engine_id", "") or "codex")[:120]

    @staticmethod
    def _usage_metadata(
        cost_controller: Any,
        *,
        engine: str,
        role: str,
    ) -> dict[str, Any]:
        """Project only numeric CostController fields across the adapter."""

        try:
            usage = cost_controller.global_tokens()
        except Exception:
            usage = {}
        try:
            snapshot = cost_controller.snapshot()
        except Exception:
            snapshot = {}
        return {
            "model": str(engine)[:120],
            "role": str(role)[:80],
            "input_tokens": int((usage or {}).get("input_tokens", 0) or 0),
            "output_tokens": int((usage or {}).get("output_tokens", 0) or 0),
            "cost_usd": float((snapshot or {}).get("global_usd", 0.0) or 0.0),
            "calls": int((snapshot or {}).get("calls", 0) or 0),
        }

    async def execute(self, job: Any) -> OfficialWorkerResult:
        """Execute one WorkerJob through upstream CLI/Sandbox primitives."""

        execution = _ActiveExecution(job=job)
        job_key = self._job_key(job)
        with self._active_lock:
            if job_key in self._active_executions:
                return OfficialWorkerResult(
                    False,
                    "REJECTED_DUPLICATE_WORKER",
                    str(getattr(job, "engine_id", "") or "worker"),
                )
            self._active_executions[job_key] = execution
            self._active_job = job
        # Hand the step thread a way back onto the loop so a finding reaches the
        # board while the Worker is still running, not only when it returns.
        with self._tool_marker_lock:
            self._job_loops[job_key] = asyncio.get_running_loop()
            if len(self._job_loops) > 64:
                for stale_key in [k for k in self._job_loops if k != job_key][:32]:
                    self._job_loops.pop(stale_key, None)
        future = asyncio.create_task(
            asyncio.to_thread(self._execute_sync, job),
            name=f"muteki-official-worker-{getattr(job, 'worker_id', 'worker')}",
        )
        execution.future = future
        future.add_done_callback(lambda _future: self._drop_execution(execution))
        try:
            # Shield the native future.  If the Coordinator cancels this
            # wrapper, the future remains awaitable while cancel_active() kills
            # the actual CLI/RCP process and reaps the thread.
            result = await asyncio.shield(future)
        except asyncio.CancelledError:
            await self._cancel_and_reap(execution)
            raise
        # The Worker's shell-emitted findings join the board HERE, on the event
        # loop, rather than from the CLI thread that captured them.  Without this
        # the board only ever saw what the model restated in its own reply, which
        # is the channel Workers do not use.  ``final=True`` also drops the
        # per-job bookkeeping the incremental flush had to keep alive.
        flushed = self._flush_tool_markers(job, final=True)
        if flushed:
            logger.info(
                "official worker tool markers published run_id=%s worker_id=%s count=%d",
                str(getattr(job, "run_id", "") or ""),
                str(getattr(job, "worker_id", "") or ""),
                flushed,
            )
        if (
            self._usage_flushed_job_key == job_key
            or job_key in self._usage_flushed_job_keys
        ):
            result = replace(
                result,
                metadata={
                    **dict(result.metadata or {}),
                    "_usage_already_recorded": True,
                },
            )
        # A completed execute() has already handed its result to the
        # Coordinator's normal usage bridge.  Retain the CostController only
        # while a cancelled asyncio task may still need recovery flushing.
        if self.evidence_bridge is None:
            return result
        if not result.success and not str(result.evidence_artifact_path or "").strip():
            return result

        try:
            refs = self.evidence_bridge.ingest(
                run_id=str(getattr(job, "run_id", "") or getattr(job, "challenge_id", "") or ""),
                worker_id=str(getattr(job, "worker_id", "") or ""),
                intent_id=str(getattr(job, "intent_id", "") or "") or None,
                result=result,
            )
            if inspect.isawaitable(refs):
                refs = await refs
            safe_refs = sanitize_evidence_refs(refs)
            if not safe_refs:
                return replace(
                    result,
                    success=False,
                    status="EVIDENCE_BRIDGE_EMPTY",
                    metadata=_bridge_failure_metadata(result, "EMPTY_REFERENCES"),
                )
            return replace(result, evidence_refs=safe_refs)
        except Exception as error:  # pragma: no cover - exercised via contract test
            # Worker failures are represented as results.  Do not propagate a
            # bridge exception into the Coordinator and do not expose its
            # message, which may contain target data or credentials.
            rollback = getattr(self.evidence_bridge, "rollback", None)
            if callable(rollback):
                try:
                    rollback_result = rollback()
                    if inspect.isawaitable(rollback_result):
                        await rollback_result
                except Exception:
                    pass
            return replace(
                result,
                success=False,
                status="EVIDENCE_BRIDGE_FAILED",
                metadata=_bridge_failure_metadata(result, type(error).__name__),
            )

    async def _cancel_and_reap(self, execution: _ActiveExecution) -> None:
        self._cancel_execution(execution)
        await self._flush_active_usage(execution)
        future = execution.future
        if future is None or future.done():
            return
        try:
            await asyncio.wait_for(
                asyncio.shield(future),
                timeout=max(0.1, float(self.config.cancel_grace_seconds)),
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return
        finally:
            await self._flush_active_usage(execution)

    async def health(self, profile: Any, *, workspace: str) -> tuple[bool, str]:
        """Run the official driver's real one-turn health probe in the Sandbox.

        The upstream Swarm filters unhealthy engines before the Race scout.  A
        host-side ``codex --version`` check is not equivalent for the container
        path: credentials, endpoint wiring and the CLI all live inside the
        worker image.  Probe there with a short bound so a broken engine cannot
        consume the whole Run-level budget before the Coordinator takes over.

        A single failed probe retires the engine for the whole Run, so one
        upstream hiccup reads as "no usable Worker" and the queue treats the
        attempt as never having happened.  Measured 2026-11-04 00:43-01:00:
        both lanes reported ``WORKER_HEALTHCHECK_TIMEOUT`` /
        ``WORKER_HEALTHCHECK_FAILED`` in alternating pairs for twenty minutes
        while the gateway was merely overloaded, and every challenge in that
        window was burned.  So a *transient* failure is retried once -- a real
        misconfiguration still fails twice and is still retired, because the
        retry only covers timeouts and transient transport, never a bad
        credential or a missing binary.
        """

        healthy, reason = await self._health_once(profile, workspace=workspace)
        if healthy or not _health_failure_is_transient(reason):
            return healthy, reason

        # Give a saturated gateway a moment instead of immediately retrying into
        # the same overload.  Short on purpose: the Run-level budget is the
        # scarcer resource here.
        #
        # WARNING, not INFO: this deployment configures no logging at all, so the
        # root logger sits at WARNING and an ``info`` line is discarded.  Measured
        # 2026-11-05 13:57 on a live Run -- the two ``probe failed`` warnings were
        # both there, but the line that explains *why there are two* was not, and
        # the retry therefore looked like it had never fired.  Whoever reads this
        # log is diagnosing a failure; the explanation belongs at a level that
        # survives.
        logger.warning(
            "Worker health probe for %s failed transiently (%s); retrying once",
            getattr(profile, "engine_id", "?"),
            reason,
        )
        await asyncio.sleep(_HEALTH_RETRY_DELAY_SECONDS)
        retried_ok, retried_reason = await self._health_once(profile, workspace=workspace)
        if retried_ok:
            return True, ""
        return False, retried_reason or reason

    async def _health_once(self, profile: Any, *, workspace: str) -> tuple[bool, str]:
        """One health probe.  Split out so the retry above cannot recurse."""

        # Fault injection for verifying the retry path end to end.
        #
        # The retry above is only reachable when the upstream actually wobbles,
        # and a wobble cannot be scheduled -- which is why it had been "verified"
        # by unit tests that stub the probe out entirely.  With this set, the
        # *first* probe of the process reports a transient failure and every
        # later one runs for real, so a live Run exercises
        # ``_health_once -> transient -> sleep -> _health_once`` without anyone
        # having to wait for a bad minute upstream.  Off unless explicitly set.
        injected = os.environ.get("MUTEKI_INJECT_HEALTH_FAILURE_ONCE", "").strip()
        if injected:
            state = globals().setdefault("_HEALTH_INJECTION_STATE", {"fired": False})
            if not state["fired"]:
                state["fired"] = True
                reason = injected if injected in _TRANSIENT_HEALTH_REASONS else "WORKER_HEALTHCHECK_TIMEOUT"
                logger.warning(
                    "Worker health probe for %s failed transiently (%s) "
                    "[injected via MUTEKI_INJECT_HEALTH_FAILURE_ONCE]",
                    getattr(profile, "engine_id", "?"),
                    reason,
                )
                return False, reason

        from muteki.solver.cli_driver import driver_for

        engine = str(getattr(profile, "engine_id", "codex") or "codex")
        raw_profile = getattr(profile, "driver_profile", None)
        driver_profile = (
            dict(raw_profile)
            if isinstance(raw_profile, Mapping) and raw_profile
            else None
        )
        driver = driver_for(driver_profile or engine)
        environment = _safe_environment(getattr(profile, "environment", {}) or {})
        credential_engine = str(
            (driver_profile or {}).get("engine") or engine
        )

        if self._container is not None:
            from muteki.solver.container_exec import run_cli_container

            # The worker image runs as root, and the Claude CLI refuses
            # ``--dangerously-skip-permissions`` under root unless IS_SANDBOX is
            # set.  Applied to the probe for the same reason it is applied to a
            # real turn: the probe must exercise the identical launch path, or a
            # healthy engine is reported dead (see the helper's docstring).
            _claude_root_permission_env(str(engine), environment)

            if self._account_root:
                try:
                    from muteki.solver.credential_accounts import runtime_env_for_engine

                    credential_env = runtime_env_for_engine(
                        credential_engine,
                        account_root=self._account_root,
                        container=True,
                        env=environment,
                    ).env
                    environment.update(_safe_environment(credential_env))
                    _drop_stale_codex_oauth_home(environment)
                except Exception:
                    return False, "CREDENTIAL_RESOLUTION_FAILED"
            # The probe must build its argv with the SAME web_access switch the
            # real Worker launch uses (see _execute_sync below).  Hardcoding
            # `False` made the probe disagree with execution for engines that
            # cannot run offline: CursorDriver.build_execute() raises
            # RuntimeError when web_access is False (Cursor has no verifiable
            # native offline-Web switch), so Cursor failed every PREPARE probe,
            # was added to _unavailable_engine_ids and never dispatched -- while
            # a real Worker with the configured web_access would have run fine.
            # Passing self.config.web_access keeps the offline intent intact (an
            # explicitly offline run still rejects Cursor) and stops the probe
            # from killing an engine execution would have accepted.
            argv = [
                str(part)
                for part in driver.build_execute(
                    "Reply exactly OK. Do not use tools.",
                    None,
                    web_access=self.config.web_access,
                    kb_access=False,
                    stream=False,
                )
            ]
            # Cursor's endpoint flag travels in the JOB environment, not in the
            # driver's global state: CursorDriver._endpoint() reads os.environ,
            # which the Coordinator process does not carry, and the real launch
            # relies on CliSolver._apply_runtime_argv to insert `--endpoint` from
            # the job env.  The probe builds its own argv and bypasses CliSolver,
            # so without this fix-up a Cursor profile pinned to a relay gateway
            # would fall back to api2.cursor.sh, fail the probe with
            # "Authentication required", and be dropped -- even though the real
            # Worker reaches the operator's endpoint.  Mirrors CliSolver exactly:
            # only insert when absent, and keep the prompt as the trailing
            # positional.
            if driver.name == "cursor":
                endpoint = str(environment.get("CURSOR_ENDPOINT") or "").strip()
                if endpoint and "--endpoint" not in argv:
                    argv = [*argv[:-1], "--endpoint", endpoint, argv[-1]]
            probe_timeout = max(
                30,
                min(int(self.config.health_timeout_seconds), int(self.config.timeout_seconds)),
            )
            try:
                result = await asyncio.to_thread(
                    run_cli_container,
                    driver,
                    argv,
                    handle=self._container,
                    cwd=str(workspace),
                    timeout=probe_timeout,
                    env=environment,
                )
            except Exception as error:
                # 一个抛出的探针（容器已销毁、UDS 断开、脚本不可执行）原本只会
                # 变成一个常量原因码，和「上游抖了一下」完全分不开。异常本身
                # 是这里唯一持久的线索，所以必须落日志。
                logger.warning(
                    "Worker health probe for %s raised: %s: %s",
                    engine,
                    type(error).__name__,
                    error,
                )
                return False, "WORKER_HEALTHCHECK_FAILED"
            if result.timed_out:
                return False, _log_health_failure(engine, "WORKER_HEALTHCHECK_TIMEOUT", result)

            if bool(getattr(result, "oom_killed", False)):
                return False, _log_health_failure(engine, "WORKER_HEALTHCHECK_FAILED", result)
            if bool(getattr(result, "cancelled", False)):
                return False, _log_health_failure(engine, "WORKER_HEALTHCHECK_FAILED", result)
            runtime_status = getattr(result, "runtime_status", None) or {}
            returncode = runtime_status.get("rc") if isinstance(runtime_status, Mapping) else None
            if returncode is not None and int(returncode) != 0:
                if _probe_answered_before_hangup(result, returncode):
                    return True, ""
                return False, _log_health_failure(
                    engine, _classify_cli_health_failure(result), result
                )
            if hasattr(result, "success") and not bool(getattr(result, "success")):
                return False, _log_health_failure(
                    engine, _classify_cli_health_failure(result), result
                )
            if not hasattr(result, "success") and not str(getattr(result, "text", "") or "").strip():
                return False, _log_health_failure(
                    engine, _classify_cli_health_failure(result), result
                )
            return True, ""

        # Local mode: run the health probe directly via subprocess
        # Use the environment directly from the profile (the API key is already
        # set by the model config decryption in _worker_profiles).  Do NOT call
        # runtime_env_for_engine here — it would overwrite the model-config
        # API key with the account store's credentials (or clear it).
        probe_env = {**os.environ, **environment}
        try:
            ok, detail = await asyncio.to_thread(
                driver.health_detail, env=probe_env
            )
            return ok, detail or "WORKER_HEALTHCHECK_FAILED"
        except Exception:
            return False, "WORKER_HEALTHCHECK_FAILED"

    def _execute_sync(self, job: Any) -> OfficialWorkerResult:
        cancel_event = self._cancel_event_for(job)
        raw_driver_profile = getattr(job, "driver_profile", None)
        driver_profile = (
            dict(raw_driver_profile)
            if isinstance(raw_driver_profile, Mapping) and raw_driver_profile
            else None
        )
        if str((driver_profile or {}).get("protocol") or "").strip() == "chat_completions":
            return self._execute_openai_compatible_sync(job, driver_profile or {})
        if self.config.protocol == "cli_solver":
            return self._execute_cli_solver_sync(job)

        from muteki.solver.cli_driver import driver_for, run_cli_streaming

        engine = str(getattr(job, "engine_id", "codex") or "codex")
        raw_driver_profile = getattr(job, "driver_profile", None)
        driver_profile = (
            dict(raw_driver_profile)
            if isinstance(raw_driver_profile, Mapping) and raw_driver_profile
            else None
        )
        workspace = str((getattr(job, "environment", {}) or {}).get("MUTEKI_WORKSPACE") or "")
        if not workspace:
            return OfficialWorkerResult(False, "FAILED", engine, metadata={"reason": "WORKSPACE_NOT_SET"})
        payload = dict(getattr(job, "payload", {}) or {})
        goal = str(getattr(job, "goal", "") or "").strip()
        intent_id = str(getattr(job, "intent_id", "") or "")
        prompt = _build_prompt(
            goal,
            intent_id,
            payload,
            _worker_flag_pattern(getattr(job, "environment", {}) or {}),
        )
        driver = _with_conversation_capture(
            driver_for(
                dict(driver_profile)
                if isinstance(driver_profile, Mapping) and driver_profile
                else engine
            ),
            workspace=workspace,
            engine=engine,
            worker_id=str(getattr(job, "worker_id", "worker")),
            intent_id=intent_id,
            handle=self._container,
        )
        argv = driver.build_execute(
            prompt,
            None,
            web_access=self.config.web_access,
            kb_access=self.config.kb_access,
            stream=True,
        )
        environment = _safe_environment(getattr(job, "environment", {}) or {})
        if self._account_root:
            try:
                from muteki.solver.credential_accounts import runtime_env_for_engine

                credential_env = runtime_env_for_engine(
                    str((driver_profile or {}).get("engine") or engine),
                    account_root=self._account_root,
                    container=self.config.backend == "container",
                    env=environment,
                ).env
                environment.update(_safe_environment(credential_env))
                _drop_stale_codex_oauth_home(environment)
            except Exception:
                return OfficialWorkerResult(
                    False,
                    "FAILED",
                    engine,
                    metadata={"reason": "CREDENTIAL_RESOLUTION_FAILED"},
                )
        # Same root guard as the probe above: a containerised Claude run needs
        # IS_SANDBOX or the CLI refuses to start, which would surface as a dead
        # Worker mid-Run rather than as a failed health check.
        _claude_root_permission_env(str(engine), environment)
        environment = _containerize_environment(environment, self._container)
        result = run_cli_streaming(
            driver,
            argv,
            cwd=workspace,
            timeout=self.config.timeout_seconds,
            on_step=lambda step: self._on_step(job, step),
            env=environment,
            cancel_event=cancel_event,
            container=self._container,
        )
        status = "TIMEOUT" if result.timed_out else "CANCELLED" if result.cancelled else "COMPLETED" if result.text else "FAILED"
        return OfficialWorkerResult(
            status == "COMPLETED",
            status,
            engine,
            output=str(result.text or "")[-12000:],
            metadata={
                "session": str(result.session or ""),
                "elapsed_s": float(result.elapsed_s or 0.0),
                "timed_out": bool(result.timed_out),
                "cancelled": bool(result.cancelled),
                "steered": bool(result.steered),
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "cost_usd": getattr(result, "cost_usd", None),
                "num_turns": getattr(result, "num_turns", None),
            },
        )

    def _execute_openai_compatible_sync(
        self,
        job: Any,
        driver_profile: Mapping[str, Any],
    ) -> OfficialWorkerResult:
        """Run the official Chat Completions Worker inside the run Sandbox.

        OpenAI-compatible providers do not speak the Codex Responses wire
        contract.  Keep their model protocol on the official Muteki
        ``LLMClient`` path and retain the same Worker -> artifact -> Evidence
        boundary as the CLI Worker.
        """

        from muteki.core.cost import CostController

        from .openai_compatible import OpenAICompatibleWorker

        environment = _safe_environment(getattr(job, "environment", {}) or {})
        target_url = str(environment.get("MUTEKI_TARGET_URL") or "").strip()
        workspace = str(
            environment.get("MUTEKI_WORKSPACE")
            or (getattr(job, "environment", {}) or {}).get("MUTEKI_WORKSPACE")
            or ""
        )
        model = str(driver_profile.get("model") or "").strip()
        base_url = str(driver_profile.get("base_url") or "").strip()
        api_key = str(environment.get("OPENAI_API_KEY") or "").strip()
        if not target_url or not workspace or not model or not base_url or not api_key:
            return OfficialWorkerResult(
                False,
                "FAILED",
                model or str(getattr(job, "engine_id", "openai_compatible")),
                metadata={"reason": "OPENAI_COMPATIBLE_PROFILE_INCOMPLETE"},
            )
        cost_controller = CostController()
        worker = OpenAICompatibleWorker(
            graph=self.shared_graph,
            container=self._container,
            workspace=workspace,
            worker_id=str(getattr(job, "worker_id", "worker")),
            intent_id=str(getattr(job, "intent_id", "") or "") or None,
            target_url=target_url,
            base_url=base_url,
            api_key=api_key,
            model=model,
            max_turns=self.config.max_turns,
            timeout_seconds=self.config.timeout_seconds,
            cost=cost_controller,
            run_id=str(getattr(job, "challenge_id", "") or ""),
            wire_api=normalize_wire_api((driver_profile or {}).get("wire_api")),
            # Without this the ``chat_completions`` Worker — which never touches
            # ``CliSolver`` — produced no WORKER_STEP at all.
            step_callback=self._emit_step_payload,
            challenge_brief=_parse_challenge_brief(environment.get("MUTEKI_CHALLENGE_BRIEF")),
        )
        self._set_native_handle(job, worker, cost_controller)
        try:
            return asyncio.run(
                worker.run(
                    goal=str(getattr(job, "goal", "") or ""),
                    payload=dict(getattr(job, "payload", {}) or {}),
                )
            )
        except Exception as error:
            return OfficialWorkerResult(
                False,
                "FAILED",
                model,
                metadata={
                    "reason": "OPENAI_COMPATIBLE_WORKER_FAILED",
                    "error_type": type(error).__name__,
                },
            )
        finally:
            self._clear_native_handle(job, worker)

    def _execute_cli_solver_sync(self, job: Any) -> OfficialWorkerResult:
        """Run the vendored official worker for one claimed Intent.

        The compatibility one-shot prompt remains available for tests and
        deployments that explicitly select ``protocol=one_shot``.  Native
        production runs use the upstream ``CliSolver`` so fact/DeadEnd/Flag
        lifecycle and the provenance gate stay owned by the official runtime.
        Only a bounded status summary crosses this adapter; the native graph
        and existing Evidence bridge remain the authorities for observations.
        """

        import asyncio
        from types import SimpleNamespace

        from muteki.core.cost import CostController
        from muteki.models.solve_graph import Challenge as UpstreamChallenge
        from muteki.solver.cli_driver import driver_for
        from muteki.solver.cli_solver import CliSolver
        from muteki.solver.result import ArtifactStore

        engine = str(getattr(job, "engine_id", "codex") or "codex")
        raw_driver_profile = getattr(job, "driver_profile", None)
        driver_profile = (
            dict(raw_driver_profile)
            if isinstance(raw_driver_profile, Mapping) and raw_driver_profile
            else None
        )
        workspace = str((getattr(job, "environment", {}) or {}).get("MUTEKI_WORKSPACE") or "")
        if not workspace:
            return OfficialWorkerResult(False, "FAILED", engine, metadata={"reason": "WORKSPACE_NOT_SET"})
        if self.shared_graph is None:
            return OfficialWorkerResult(False, "FAILED", engine, metadata={"reason": "NATIVE_GRAPH_NOT_SET"})

        payload = dict(getattr(job, "payload", {}) or {})
        goal = str(getattr(job, "goal", "") or "").strip()
        intent_id = str(getattr(job, "intent_id", "") or "")
        arguments = payload.get("arguments")
        request = arguments.get("request") if isinstance(arguments, Mapping) else None
        if not isinstance(request, Mapping):
            request = arguments if isinstance(arguments, Mapping) else {}
        target = str(request.get("url") or "").strip() or str(
            (getattr(job, "environment", {}) or {}).get("MUTEKI_TARGET_URL") or ""
        ).strip() or None
        environment = _safe_environment(getattr(job, "environment", {}) or {})
        configured_target_host = (
            self.config.container_target_host
            or environment.get("MUTEKI_CONTAINER_TARGET_HOST")
            or os.environ.get("MUTEKI_CONTAINER_TARGET_HOST")
        )
        if self.config.backend == "container" and target and configured_target_host:
            container_target = _container_target_url(
                target,
                replacement_host=str(configured_target_host),
            )
            if container_target != target:
                payload = _rewrite_target_urls(payload, target, container_target)
                target = container_target
                environment["MUTEKI_TARGET_URL"] = container_target
        safe_contract = _safe_prompt_payload(payload)
        contract = json.dumps(safe_contract, ensure_ascii=False, separators=(",", ":"))[:3000]
        typed_contract = _typed_handoff_contract(payload)
        typed_json = (
            json.dumps(typed_contract, ensure_ascii=False, separators=(",", ":"))
            if typed_contract is not None
            else ""
        )
        intent_goal = (
            f"{goal or 'advance one bounded action'}\n"
            "Stay within this action contract and do not switch tool domains: "
            f"{contract}"
        )
        if typed_json:
            intent_goal += (
                "\nActually execute the bounded action now against the declared target; do not "
                "stop after reading the board or merely describe a possible request. Use "
                "the container shell and real command output, while keeping the same session "
                "where applicable. After the bounded action, emit one short non-marker "
                "safe witness sentence naming only the action, approved field/capability, "
                "and boolean/status outcome (never response data), then emit exactly two "
                "consecutive machine-readable lines in your reply: first "
                "OBSERVATION_JSON=<the same JSON object>, then "
                "VERIFIED_FACT=<the same JSON object>. Fill only values observed from the "
                "action; use JSON booleans/integers, not placeholders. The object must "
                "contain only the fields shown below and must not contain response bodies, "
                "credentials, cookies, tokens, secrets, or flags. If no safe observation "
                "exists, emit DEADEND= with a short reason instead. Do not turn the "
                "verified fact into prose: before replying, write the same JSON object "
                "to the official Blackboard with `python3 \"$MUTEKI_BLACKBOARD_SCRIPT\" "
                "write-fact '<json-object>' --verified`. This Blackboard write is the "
                "machine-readable handoff consumed by the next Coordinator turn.\n"
                f"Typed Fact shape: {typed_json}"
            )
        # Every intent needs the flag handoff.  The Coordinator cannot predict
        # which bounded action recovers the flag, so the hint must not depend on
        # a typed handoff nor on a single nominated tool name.
        flag_gate_instruction = _flag_gate_instruction(payload, _worker_flag_pattern(environment))
        if flag_gate_instruction:
            intent_goal += "\n" + flag_gate_instruction
        challenge = UpstreamChallenge(
            id=str(getattr(job, "challenge_id", "") or "native-challenge"),
            name=goal[:160] or "Native Muteki action",
            category="web",
            description="Execute one bounded Solver Intent against the declared target.",
            target=target,
            # The Coordinator seeds the challenge's real flag shape; a hardcoded
            # banner made the official gate reject every non-``flag`` challenge.
            flag_format=_worker_flag_pattern(environment),
        )
        if self._account_root:
            try:
                from muteki.solver.credential_accounts import runtime_env_for_engine

                credential_env = runtime_env_for_engine(
                    _credential_engine_name(engine, driver_profile),
                    account_root=self._account_root,
                    container=self.config.backend == "container",
                    env=environment,
                ).env
                environment.update(_safe_environment(credential_env))
                _drop_stale_codex_oauth_home(environment)
            except Exception:
                return OfficialWorkerResult(
                    False,
                    "FAILED",
                    engine,
                    metadata={"reason": "CREDENTIAL_RESOLUTION_FAILED"},
                )

        artifact_key = sha256(
            (intent_id or str(getattr(job, "worker_id", "worker"))).encode(
                "utf-8", errors="replace"
            )
        ).hexdigest()[:16]
        artifact_store = ArtifactStore(
            root=str(Path(workspace) / ".muteki-artifacts" / artifact_key)
        )
        # Keep the official Muteki cost controller at the native Worker
        # boundary.  The vendored CliSolver already feeds every CLI turn into
        # this ledger; the adapter only projects its numeric snapshot into the
        # existing durable RunEvent stream after the Worker exits.
        cost_controller = CostController()
        solver = CliSolver(
            spec=SimpleNamespace(solver_id=str(getattr(job, "worker_id", "native-worker"))),
            challenge=challenge,
            shared_graph=self.shared_graph,
            driver=_attach_step_observer(
                _with_conversation_capture(
                    driver_for(
                        dict(driver_profile)
                        if isinstance(driver_profile, Mapping) and driver_profile
                        else engine
                    ),
                    workspace=workspace,
                    engine=engine,
                    worker_id=str(getattr(job, "worker_id", "worker")),
                    intent_id=intent_id,
                    handle=self._container,
                ),
                lambda step: self._on_step(job, step),
            ),
            engine=engine,
            max_turns=self.config.max_turns,
            timeout=self.config.timeout_seconds,
            workdir=workspace,
            web_access=self.config.web_access,
            kb=self.config.kb_access,
            # Official Muteki uses a whole-challenge bootstrap worker when
            # Reason has no new Intent after a barren direction.  The
            # compatibility Coordinator marks this explicitly by role; all
            # claimed Intents retain the official explore mode.
            # Official Swarm launches Race as a bootstrap scout.  ``race`` is
            # a Coordinator role, not an Intent-scoped explore mode.
            mode=(
                "bootstrap"
                if str(getattr(job, "role", "")) in {"bootstrap", "race"}
                else "explore"
            ),
            intent_goal=intent_goal,
            intent_id=intent_id,
            conclude_timeout=min(120, self.config.timeout_seconds),
            lifecycle_scope="worker",
            container=self._container,
            worker_env=environment,
            artifacts=artifact_store,
            cost=cost_controller,
        )
        self._set_native_handle(job, solver, cost_controller)
        try:
            outcome = asyncio.run(solver.run())
        except Exception as error:
            metadata = self._usage_metadata(
                cost_controller,
                engine=engine,
                role="worker",
            )
            metadata.update(
                {
                    "reason": "CLI_SOLVER_FAILED",
                    "error_type": type(error).__name__,
                }
            )
            return OfficialWorkerResult(
                False,
                "FAILED",
                engine,
                metadata=metadata,
            )
        finally:
            self._clear_native_handle(job, solver)

        metadata = self._usage_metadata(
            cost_controller,
            engine=engine,
            role="worker",
        )
        metadata.update(
            {
                "solved": bool(getattr(outcome, "solved", False)),
                "steps": int(getattr(outcome, "steps", 0) or 0),
                "session": str(getattr(outcome, "session", "") or ""),
            }
        )
        return OfficialWorkerResult(
            True,
            "COMPLETED",
            engine,
            output="official CliSolver completed one bounded Intent",
            metadata=metadata,
            evidence_artifact_path=_largest_artifact_path(artifact_store.root),
        )

    def _emit_step_payload(self, payload: Mapping[str, Any]) -> None:
        """Forward one already-sanitised Worker step to the step sink.

        The single place ``step_callback`` is invoked, so both execution paths
        (CLI/``CliSolver`` and Chat Completions) share one contract.  Failure
        isolation lives here rather than at each call site: observability is
        never worth failing a Worker over.
        """

        callback = self.step_callback
        if callback is None:
            return
        try:
            callback(dict(payload))
        except Exception:
            pass

    def _on_step(self, job: Any, step: Any) -> None:
        """Forward one CLI stream step, including the command and its output.

        The upstream ``StreamStep`` already carries both: ``text`` holds the
        invoked command for ``kind == "tool"``, ``raw`` holds the full
        (untruncated) output for ``kind == "tool_result"``.  They used to be
        dropped here on the theory that tool text may carry credentials, which
        left the durable trace unable to answer *which* command produced a dead
        end.  They are now redacted at this boundary instead of discarded.
        """

        kind = str(getattr(step, "kind", "") or "")
        payload: dict[str, str] = {
            "worker_id": str(getattr(job, "worker_id", "")),
            "kind": kind,
            "tool": str(getattr(step, "tool", "")),
            "session": str(getattr(step, "session", "")),
        }
        call_id = str(getattr(step, "call_id", "") or "")
        if call_id:
            payload["call_id"] = call_id
        # ``__init__`` may be bypassed by a lightweight adapter in tests/plugins;
        # the pairing map is an optimisation, never a requirement.
        pending = getattr(self, "_pending_commands", None)
        if pending is None:
            pending = self._pending_commands = {}
        if kind == "tool":
            # Prefer the parser's untruncated copy.  ``text`` is the 300-char deck
            # preview; recording it (as this used to) clipped every multi-line
            # payload before it reached the trace, so nothing long could ever be
            # replayed from the event stream.
            command = str(getattr(step, "command", "") or getattr(step, "text", "") or "")
            if command:
                payload["command"] = redact(command, limit=COMMAND_LIMIT)
                # Bounded map: a CLI round issues tens of calls, never thousands,
                # and the key is dropped as soon as its result arrives.
                if len(pending) > 64:
                    pending.clear()
                pending[call_id or f"#{len(pending)}"] = payload["command"]
        elif kind == "tool_result":
            output = str(getattr(step, "raw", "") or getattr(step, "text", "") or "")
            # Pop the paired command BEFORE capturing markers.  A fact whose
            # producing command is not recorded is an unattributable claim: the
            # board cannot say what was run to observe it, and the trajectory
            # layer has to guess by matching text back onto earlier steps.
            paired = pending.pop(call_id or f"#{len(pending) - 1}", "")
            if paired and "command" not in payload:
                payload["command"] = paired
            if output:
                payload["result_excerpt"] = redact(output, limit=RESULT_LIMIT)
                # The Worker's structured findings are printed through the shell,
                # so they have to be read from REAL command output -- the reply
                # text the solver scans never contains them.  Buffered here (step
                # thread) and written to the board in ``execute`` (event loop).
                self._capture_tool_markers(job, output, command=paired)
        self._emit_step_payload(payload)

    #: A Worker rarely has more than a handful of genuine findings; the cap keeps
    #: a looping Worker from filling the board -- and the shared graph's
    #: 60-candidate bucket for this actor -- with restatements.
    _TOOL_MARKER_CAP = 24

    def _capture_tool_markers(self, job: Any, output: str, *, command: str = "") -> None:
        """Buffer the markers a shell command PRINTED.  Step-thread safe.

        ``command`` is the shell invocation whose stdout carried the marker.  It
        is carried through to ``add_evidence(witness=...)`` so a board entry can
        always name the command that produced it.  Before this, the paired
        command was already sitting in ``_pending_commands`` but was popped
        *after* the capture call, so every fact reached the graph unattributed.
        """

        facts, dead_ends = _tool_output_markers(output)
        if not facts and not dead_ends:
            return
        provenance = _marker_provenance(command)
        key = self._job_key(job)
        should_drain = False
        with self._tool_marker_lock:
            # A cancelled Worker never reaches ``execute``'s flush, so its buffer
            # would otherwise live for the rest of the Run.  Bound the table
            # instead of hooking the future's done-callback: that callback can
            # fire before the flush and would empty a live buffer.
            if len(self._tool_markers) > 64:
                for stale in [k for k in self._tool_markers if k != key][:32]:
                    self._tool_markers.pop(stale, None)
            seen = self._published_markers.get(key)
            slot = self._tool_markers.setdefault(key, {"facts": [], "dead_ends": []})
            for bucket, values in (("facts", facts), ("dead_ends", dead_ends)):
                room = slot[bucket]
                for value in values:
                    dedupe = _marker_key(value)
                    if seen and dedupe in seen:
                        continue
                    # Normalised comparison, not exact string equality: the same
                    # finding printed with different spacing or casing is one
                    # finding.  Keep the FIRST spelling so the board stays stable
                    # across replay.
                    if len(room) < self._TOOL_MARKER_CAP and all(
                        _marker_key(item["value"]) != dedupe for item in room
                    ):
                        room.append(
                            {"value": value, "command": command, "provenance": provenance}
                        )
                        should_drain = True
            loop = self._job_loops.get(key)
        if not should_drain or loop is None or loop.is_closed():
            return
        try:
            # Runs ``_flush_tool_markers`` on the event loop, which is the only
            # thread allowed to write the board.  Fire-and-forget on purpose:
            # the step callback must never block on a database write.
            loop.call_soon_threadsafe(self._flush_tool_markers, job)
        except RuntimeError:
            # The loop shut down between the check and the call; ``execute``'s
            # final flush (or the Run teardown) owns whatever is left.
            pass

    def _flush_tool_markers(self, job: Any, *, final: bool = False) -> int:
        """Publish buffered shell markers to the board.  Event-loop safe.

        Facts go in as candidates (``verified=False``): the marker proves the
        Worker PRINTED a claim, not that the claim is true -- ``echo`` can put
        anything on the board.  Dead ends are advisory by construction.  Returns
        how many entries were written, for the caller's log line only.

        Calling this repeatedly is the point: it is driven from the step thread
        through ``call_soon_threadsafe``, so a fact discovered at minute 3 is on
        the board at minute 3.  ``final=True`` releases the per-job dedupe and
        loop bookkeeping once the Worker is done.
        """

        key = self._job_key(job)
        with self._tool_marker_lock:
            slot = self._tool_markers.pop(key, None)
            seen = self._published_markers.setdefault(key, set())
            if final:
                self._published_markers.pop(key, None)
                self._job_loops.pop(key, None)
        if not slot:
            return 0
        graph = self.shared_graph
        if graph is None:
            return 0
        actor = str(getattr(job, "worker_id", "") or "worker")
        intent_id = str(getattr(job, "intent_id", "") or "")
        written = 0
        for item in slot["facts"]:
            value = item["value"]
            dedupe = _marker_key(value)
            if dedupe in seen:
                continue
            if self._publish_fact(
                graph,
                actor=actor,
                value=value,
                intent_id=intent_id,
                witness=item.get("command", ""),
                provenance=item.get("provenance", "unknown"),
            ):
                seen.add(dedupe)
                written += 1
        for item in slot["dead_ends"]:
            value = item["value"]
            dedupe = _marker_key(value)
            marker = f"dead_end:{dedupe}"
            if marker in seen:
                continue
            if self._publish_dead_end(graph, actor=actor, value=value):
                seen.add(marker)
                written += 1
        if len(seen) > 2 * self._TOOL_MARKER_CAP:
            # A Worker that prints the same shape of line hundreds of times must
            # not grow this set without bound before ``execute`` returns.
            seen.clear()
        return written

    @staticmethod
    def _publish_fact(
        graph: Any,
        *,
        actor: str,
        value: str,
        intent_id: str,
        witness: str = "",
        provenance: str = "unknown",
    ) -> bool:
        """Write one candidate fact through whichever board API this graph has.

        Production hands the adapter the RAW official ``SharedGraph``
        (``UpstreamRuntimeGraph.native_graph()`` returns ``self._graph``), whose
        writer is ``add_evidence``.  The compatibility facade above it exposes
        ``add_fact`` instead.  Probing both is not belt-and-braces: an
        ``add_fact``-only implementation would be a silent no-op in production,
        which is exactly the "green tests, dead feature" failure this project has
        already paid for once.

        ``witness`` names the shell command whose output carried the marker, and
        ``verifier`` records how the adapter knows: ``observed`` when a probe's
        own output produced the line, ``inferred`` when the Worker merely echoed
        it.  Both stay ``verified=False`` -- a marker proves the claim was
        PRINTED, never that it is true.  The attribution is what makes a board
        entry auditable; ``confidence`` differs only so downstream ranking can
        tell the two apart.
        """

        add_evidence = getattr(graph, "add_evidence", None)
        if callable(add_evidence):
            kwargs: dict[str, Any] = {
                "actor": actor,
                "source": actor,
                "fact": value,
                "verified": False,
                "confidence": 0.7 if provenance == "observed" else 0.4,
                "intent_id": intent_id or None,
            }
            if witness:
                kwargs["witness"] = witness[:WITNESS_LIMIT]
            if provenance and provenance != "unknown":
                kwargs["verifier"] = provenance
            try:
                add_evidence(**kwargs)
                return True
            except TypeError:
                # A compatibility facade may not accept the attribution fields
                # at all.  Drop them and retry rather than losing the fact: an
                # unattributed fact still beats a silently discarded one.
                for optional in ("witness", "verifier"):
                    kwargs.pop(optional, None)
                try:
                    add_evidence(**kwargs)
                    return True
                except Exception:  # noqa: BLE001
                    return False
            except Exception:  # noqa: BLE001 - a board write must not end a Run
                return False
        add_fact = getattr(graph, "add_fact", None)
        if callable(add_fact):
            try:
                add_fact(actor=actor, content=value, verified=False)
                return True
            except Exception:  # noqa: BLE001
                return False
        return False

    @staticmethod
    def _publish_dead_end(graph: Any, *, actor: str, value: str) -> bool:
        """``add_dead_end`` takes ``reason`` on the official graph, ``description``
        on the facade -- and the official one is what production passes."""

        add_dead_end = getattr(graph, "add_dead_end", None)
        if not callable(add_dead_end):
            return False
        for field_name in ("reason", "description"):
            try:
                add_dead_end(actor=actor, **{field_name: value})
                return True
            except TypeError:
                continue
            except Exception:  # noqa: BLE001
                return False
        return False


#: Where the run workspace is bind-mounted inside the Worker container.  Kept in
#: one place because it is used in BOTH directions: forward when projecting a
#: host path into the container (``_containerize_environment``) and backward when
#: resolving a container path for a host-side writer
#: (``_conversation_host_workspace``).
_CONTAINER_WORKSPACE = "/home/kali/workspace"


def _conversation_host_workspace(workspace: str, handle: Any | None) -> str:
    """Resolve a Worker workspace to a HOST path the backend can actually write.

    With ``worker_backend == "upstream_container"`` the Coordinator sets
    ``MUTEKI_WORKSPACE`` to the *container* path (``/home/kali/workspace``),
    because that is what the CLI running inside the container must see.  The
    transcript sink, however, is written by the HOST process, so a container
    path would be silently re-rooted (``Path('/home/kali/...')`` on Windows
    lands under the current drive) and the evidence would be unfindable by the
    report writer.  Translate back through the container handle's real bind
    source; when there is no handle (bare host subprocess / legacy docker exec)
    the workspace is already a host path and passes through untouched.
    """

    if handle is None:
        return workspace
    host_workspace = getattr(handle, "host_workspace", None)
    container_workspace = getattr(handle, "CONTAINER_WORKSPACE", None) or _CONTAINER_WORKSPACE
    if not host_workspace:
        return workspace
    normalized = str(workspace).replace("\\", "/").rstrip("/")
    if normalized != container_workspace and not normalized.startswith(
        container_workspace + "/"
    ):
        return workspace
    rel = normalized[len(container_workspace):].lstrip("/")
    return str(Path(str(host_workspace)) / rel) if rel else str(host_workspace)


def _with_conversation_capture(
    driver: Any,
    *,
    workspace: str,
    engine: str,
    worker_id: str,
    intent_id: str,
    handle: Any | None = None,
) -> Any:
    """Bind this Worker's CLI transcript sink to its OWN driver copy.

    The Worker adapter is the only caller that knows the Run workspace, and the
    workspace is what makes the transcript durable: it is bind-mounted into the
    worker container, so files written here survive ``docker rm -f`` (engine
    session files under the container HOME do not).

    The driver is **copied** because ``driver_for`` returns the process-wide
    singleton for a bare engine name — mutating it would cross-wire two
    concurrently finishing runs into one another's evidence directory.
    """

    try:
        import copy

        from muteki.solver.cli_driver import attach_conversation_sink

        own = copy.copy(driver)
        host_workspace = _conversation_host_workspace(workspace, handle)
        directory = Path(host_workspace) / "evidence" / "cli-conversations"
        directory.mkdir(parents=True, exist_ok=True)
        attach_conversation_sink(
            own,
            directory=str(directory),
            label=f"{engine}-{worker_id}-{intent_id or 'intent'}",
        )
        return own
    except Exception:
        return driver


def _attach_step_observer(driver: Any, observer: Callable[[Any], None] | None) -> Any:
    """Forward every parsed CLI stream step to a Worker observability callback.

    The vendored ``CliSolver`` runs its own in-method ``on_step`` and offers no
    injection point, so a Worker that goes through ``protocol=cli_solver``
    (production) used to be a black box: ``EventType.WORKER_STEP`` existed and
    the Coordinator callback was wired, but nothing ever called it.  Both
    streaming entry points read steps from ``driver.parse_stream_steps``, so
    shadowing that ONE method on the per-Worker driver copy is enough to make
    the Worker observable without touching the vendored solver.

    The observer is deliberately failure-isolated: a raised exception would
    propagate into ``cli_solver``'s marker handling, so a broken observability
    path must degrade to silence rather than break the Worker.  Only the
    already-sanitised identity fields (kind/tool/session) are exposed — never
    the step text or tool output.
    """

    if observer is None:
        return driver
    original = getattr(driver, "parse_stream_steps", None)
    if not callable(original):
        return driver

    def _observed(line: str) -> Any:
        steps = original(line)
        try:
            for step in steps or ():
                try:
                    observer(step)
                except Exception:
                    pass
        except Exception:
            pass
        return steps

    try:
        # ``driver_for`` returns a process-wide singleton, hence the caller
        # always hands us the per-Worker ``copy.copy`` — never mutate the
        # shared instance.
        setattr(driver, "parse_stream_steps", _observed)
    except Exception:
        return driver
    return driver


def _build_prompt(goal: str, intent_id: str, payload: dict[str, Any], flag_pattern: str = "") -> str:
    instruction = str(payload.get("instruction") or payload.get("prompt") or "").strip()
    lines = [
        "You are one single-shot Muteki Worker.",
        f"Intent: {intent_id or 'unassigned'}",
        f"Goal: {goal or 'inspect the shared blackboard and advance one bounded route'}",
        "Read the shared blackboard first. Work only on this intent.",
        "Write evidence-backed facts, dead ends, and intent conclusions through the shared blackboard skill.",
        'Use "$MUTEKI_BLACKBOARD_SCRIPT" with "$MUTEKI_BLACKBOARD_DB" for every Blackboard read/write.',
        "Do not claim completion from an unverified candidate or model text.",
        "Respect the action contract below. Do not invent another tool domain or exceed its request limit.",
    ]
    if instruction:
        lines.append(f"Additional bounded instruction: {instruction[:1200]}")
    contract = _safe_prompt_payload(payload)
    if contract:
        lines.append(
            "Safe action contract: "
            + json.dumps(contract, ensure_ascii=False, separators=(",", ":"))[:3000]
        )
    handoff = _action_handoff_contract(payload)
    if handoff:
        typed_handoff = _typed_handoff_contract(payload)
        lines.extend(
            [
                "Required Blackboard handoff: after the bounded action, write exactly one "
                "candidate fact through the skill and then stop.",
                "Use this JSON shape, filling only values observed from the action:",
                "python3 \"$MUTEKI_BLACKBOARD_SCRIPT\" write-fact '"
                + json.dumps(handoff, ensure_ascii=False, separators=(",", ":"))
                + "'",
                "The fact must not contain response bodies, credentials, cookies, tokens, "
                "secrets, or flags. The Coordinator owns intent closure.",
                "Persist the same JSON object through the official Blackboard before the "
                "marker reply: `python3 \"$MUTEKI_BLACKBOARD_SCRIPT\" write-fact '<json-object>' "
                "--verified`.",
            ]
        )
        if typed_handoff is not None:
            lines.extend(
                [
                    "Also emit one short non-marker safe witness sentence naming only the "
                    "action, approved field/capability, and boolean/status outcome (never "
                    "response data), then emit exactly two consecutive machine-readable "
                    "witness lines in the reply: OBSERVATION_JSON=<the same JSON object>, then "
                    "VERIFIED_FACT=<the same JSON object>. Fill only values observed from "
                    "the action; use JSON booleans/integers, not placeholders. The object "
                    "must contain only this safe shape. Typed Fact shape:",
                    json.dumps(typed_handoff, ensure_ascii=False, separators=(",", ":")),
                ]
            )
    # Every intent can surface the flag: the Coordinator cannot always predict
    # which bounded action recovers it, and previously this hint was limited to
    # one tool name *and* required a typed handoff -- so an exploration intent
    # (for example a model-inversion oracle) was never told how to report a
    # flag it had already seen.
    flag_gate_instruction = _flag_gate_instruction(payload, flag_pattern)
    if flag_gate_instruction:
        lines.append(flag_gate_instruction)
    return "\n".join(lines)


def _flag_gate_instruction(payload: Mapping[str, Any], flag_pattern: str = "") -> str:
    """Return the completion instruction for recovering the challenge flag.

    The recovered value stays inside the official Worker and its Blackboard
    gate.  It must never be copied into the typed observation consumed by the
    host-side Strategy adapter.

    ``payload`` is accepted for call-site symmetry only: the hint no longer
    depends on the nominated tool, because a flag can surface from any bounded
    action (a model-inversion oracle is one example).
    """

    del payload  # retained for call-site symmetry; the hint is tool-agnostic
    shape = str(flag_pattern or "").strip() or r"flag\{...\}"
    return (
        "If the real command output contains a flag -- either matching the "
        f"declared challenge format `{shape}` OR any `BANNER{{...}}` shape such as "
        "`CTF2{...}` / `XXCTF{...}` / `XXX{...}` (the declared banner is a hint, "
        "not a filter: a challenge is often configured with the wrong one) -- "
        "submit that exact candidate together with the raw "
        "output through the official Blackboard `write-flag` command "
        "(`python3 \"$MUTEKI_BLACKBOARD_SCRIPT\" write-flag '<candidate>' "
        "'<raw output>'`) so the official provenance gate decides it. Never "
        "invent a candidate: the gate rejects anything that does not appear "
        "verbatim in the captured output. Never place the candidate, raw "
        "output, or any secret in OBSERVATION_JSON or VERIFIED_FACT; those "
        "typed fields carry only the bounded action status. Submitting the flag "
        "to the Blackboard is what lets the Coordinator stop and score the Run."
    )


#: ``VERIFIED_FACT=`` / ``DEADEND=`` are a Worker's only structured channel to the
#: board, and the goal text never said WHERE to emit them.  Workers therefore
#: ``echo`` them from the shell.  Measured 2026-10-29 on run ``4b65af58`` (Web6):
#: four ``VERIFIED_FACT=`` echoes in real command output, and the Run finished
#: with ``fact_added = 0`` -- the vendored solver parses markers out of the
#: model's REPLY text only (``_stream_markers(all_text)``), and its live
#: tool-output parsing is behind ``self.bus is not None``, which the arena run
#: never sets.  So every shell-emitted marker was dropped on the floor.
_MARKER_FACT_RE = re.compile(r"^VERIFIED_FACT=[ \t]*(\S.*)$")
_MARKER_DEADEND_RE = re.compile(r"^(?:DEADEND|DEAD_END)=[ \t]*(\S.*)$")

#: A marker whose payload is itself a REGEX means the Worker was grepping the
#: transcript for its own marker, not reporting a finding.  This is the same
#: shape that once registered ``bl_|VERIFIED_FACT=.*L4`` as a flag (run-11550);
#: here it would put a pattern on the board as if it were an observation.
_MARKER_PATTERN_SHAPES = (".*", "(?", "[^", "|")


def _tool_output_markers(text: str, *, cap: int = 200) -> tuple[list[str], list[str]]:
    """``(facts, dead_ends)`` the Worker printed through a SHELL command.

    Only line-anchored markers count: a marker embedded mid-sentence is the
    Worker quoting itself, not reporting.  Values are truncated to the width the
    board keeps, and a regex-shaped payload is dropped (see
    ``_MARKER_PATTERN_SHAPES``).
    """

    facts: list[str] = []
    dead_ends: list[str] = []
    if "VERIFIED_FACT=" not in text and "DEADEND=" not in text and "DEAD_END=" not in text:
        # Cheap pre-filter: this runs on EVERY tool result, and a large listing can
        # be tens of thousands of lines.
        return facts, dead_ends
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or len(stripped) > 8 * cap:
            continue
        match = _MARKER_FACT_RE.match(stripped)
        bucket = facts
        if match is None:
            match = _MARKER_DEADEND_RE.match(stripped)
            bucket = dead_ends
        if match is None:
            continue
        value = match.group(1).strip()[:cap]
        if not value or any(shape in value for shape in _MARKER_PATTERN_SHAPES):
            continue
        if value not in bucket:
            bucket.append(value)
    return facts, dead_ends


#: Audit trail for a board fact: the command that produced it is stored on the
#: graph as ``witness``.  Bounded because a shell command can be a 20 KB heredoc
#: payload, and the board row is re-read on every snapshot.
WITNESS_LIMIT = 400


def _marker_key(value: str) -> str:
    """Normalised identity of a marker, used for dedupe only.

    Whitespace and casing are ignored when deciding whether the same finding was
    already published (``Flask app`` and ``flask  app`` are one finding).  The
    first spelling survives so replay stays byte-stable.
    """

    return re.sub(r"\s+", "", value or "").casefold()


#: Shell shapes that PRINT a string rather than OBSERVE one.  A Worker that
#: ``echo``\\s ``VERIFIED_FACT=...`` is telling the board what it believes; a
#: Worker whose ``curl`` output happens to contain the line has witnessed it.
_ECHO_ONLY_RE = re.compile(
    r"^\s*(?:echo|printf|cat\s*<<|tee)\b"
    r"|python[0-9.]*\s+-c\s+[\"'](?:print|sys\.stdout)",
    re.IGNORECASE,
)


def _marker_provenance(command: str) -> str:
    """``observed`` / ``inferred`` / ``unknown`` for a marker's producing command.

    A heuristic, not a proof: a Worker can pipe its own echo through ``grep`` and
    land in ``observed``.  The distinction is still worth having -- before it,
    every fact carried identical confidence, so the board could not tell a probe
    result from a self-report, and neither could the diagnosis layer.
    """

    text = (command or "").strip()
    if not text:
        return "unknown"
    if _ECHO_ONLY_RE.search(text):
        return "inferred"
    return "observed"


_PROMPT_BLOCKED_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "ground_truth",
        "password",
        "raw",
        "raw_result",
        "secret",
        "token",
    }
)


def _safe_prompt_payload(value: Any, *, _key: str = "") -> Any:
    """Project bounded action metadata without copying secrets or observations."""

    if _key.casefold() in _PROMPT_BLOCKED_KEYS:
        return None
    # Structured request bodies carry the schema the Worker needs (for example
    # asset_no/department).  Preserve mappings recursively, but never forward
    # an opaque string body that could contain credentials or raw target data.
    if _key.casefold() == "body" and not isinstance(value, Mapping):
        return None
    if isinstance(value, Mapping):
        projected: dict[str, Any] = {}
        for key, item in value.items():
            safe = _safe_prompt_payload(item, _key=str(key))
            if safe is not None:
                projected[str(key)] = safe
        return projected
    if isinstance(value, (list, tuple)):
        return [item for item in (_safe_prompt_payload(item) for item in value) if item is not None][:32]
    if isinstance(value, (str, int, float, bool)):
        return value[:600] if isinstance(value, str) else value
    return None


def _action_handoff_contract(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return a safe, machine-readable fact shape for one Worker handoff."""

    tool_name = str(payload.get("tool_name") or payload.get("tool") or "").strip()
    arguments = payload.get("arguments")
    if not tool_name or not isinstance(arguments, Mapping):
        return None
    if tool_name == "sql_boolean_compare":
        return {
            "tool": tool_name,
            "test_field": str(arguments.get("test_field") or ""),
            "success": "<true-or-false>",
            "boolean_oracle_confirmed": "<true-or-false>",
            "oracle_verified": "<true-or-false>",
            "request_count": "<integer>",
        }
    if tool_name == "oracle_expression_calibration":
        return {
            "tool": tool_name,
            "success": "<true-or-false>",
            "oracle_verified": "<true-or-false>",
            "capabilities": ["<capability>"],
            "extraction_strategy": "<identifier>",
            "request_count": "<integer>",
        }
    if tool_name == "mysql_metadata_discovery":
        return {
            "tool": tool_name,
            "success": "<true-or-false>",
            "stage": "<database-or-tables-or-columns>",
            "target_expression": "<DATABASE()-or-information_schema.tables-or-information_schema.columns>",
            "database": "<identifier>",
            "tables": ["<identifier>"],
            "columns": ["<identifier>"],
            "request_count": "<integer>",
        }
    if tool_name == "boolean_config_extract":
        return {
            "tool": tool_name,
            "success": "<true-or-false>",
            "extraction_verified": "<true-or-false>",
            "verification_method": "<artifact_witness-or-blackboard_flag_gate>",
            "request_count": "<integer>",
        }
    return {
        "tool": tool_name,
        "success": "<true-or-false>",
        "status_code": "<integer-or-null>",
        "endpoint": "<same-origin-path>",
        "summary": "<safe-observation-summary>",
    }


def _typed_handoff_contract(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """Build the strict JSON shape accepted by the native observation seam."""

    tool_name = str(payload.get("tool_name") or payload.get("tool") or "").strip()
    arguments = payload.get("arguments")
    if not tool_name or not isinstance(arguments, Mapping):
        return None
    if tool_name == "sql_boolean_compare":
        return {
            "tool": tool_name,
            "test_field": str(arguments.get("test_field") or ""),
            "success": "<true-or-false>",
            "boolean_oracle_confirmed": "<true-or-false>",
            "oracle_verified": "<true-or-false>",
            "request_count": "<integer>",
        }
    if tool_name == "oracle_expression_calibration":
        return {
            "tool": tool_name,
            "success": "<true-or-false>",
            "oracle_verified": "<true-or-false>",
            "capabilities": ["<capability>"],
            "extraction_strategy": "<identifier>",
            "request_count": "<integer>",
        }
    if tool_name == "mysql_metadata_discovery":
        return {
            "tool": tool_name,
            "success": "<true-or-false>",
            "stage": "<database-or-tables-or-columns>",
            "target_expression": "<DATABASE()-or-information_schema.tables-or-information_schema.columns>",
            "database": "<identifier>",
            "tables": ["<identifier>"],
            "columns": ["<identifier>"],
            "request_count": "<integer>",
        }
    if tool_name == "boolean_config_extract":
        return {
            "tool": tool_name,
            "success": "<true-or-false>",
            "extraction_verified": "<true-or-false>",
            "verification_method": "<artifact_witness-or-blackboard_flag_gate>",
            "request_count": "<integer>",
        }
    return {
        "tool": tool_name,
        "success": "<true-or-false>",
        "status_code": "<integer-or-null>",
        "endpoint": "<same-origin-path>",
    }


def _container_target_url(value: str, *, replacement_host: str) -> str:
    """Make a host-published private target reachable from Docker.

    The official Worker image runs in a sibling container.  On Docker Desktop,
    a challenge published only on the host's loopback/private interface is
    reachable through ``host.docker.internal`` but not through the host IP
    embedded in the challenge URL.  Rewrite only private/loopback IP targets;
    public URLs and Docker service names remain untouched.
    """

    original = str(value or "").strip()
    host = str(replacement_host or "").strip()
    if not original or not host:
        return original
    parsed = urlsplit(original)
    hostname = parsed.hostname
    if parsed.scheme not in {"http", "https"} or not hostname:
        return original
    folded = hostname.casefold()
    private = folded in {"localhost", "127.0.0.1", "0.0.0.0"}
    if not private:
        try:
            private = ipaddress.ip_address(hostname).is_private
        except ValueError:
            private = False
    if not private:
        return original
    try:
        port = parsed.port
    except ValueError:
        return original
    netloc = host
    if ":" in host and not host.startswith("["):
        netloc = f"[{host}]"
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


_URL_PAYLOAD_KEYS = frozenset({"url", "target", "endpoint", "uri", "request_url"})


def _rewrite_target_urls(value: Any, source_url: str, replacement_url: str, *, _key: str = "") -> Any:
    """Rewrite same-origin request URLs in a bounded Intent payload."""

    source = urlsplit(str(source_url or ""))
    replacement = urlsplit(str(replacement_url or ""))
    if isinstance(value, Mapping):
        return {
            str(key): _rewrite_target_urls(
                item,
                source_url,
                replacement_url,
                _key=str(key),
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _rewrite_target_urls(item, source_url, replacement_url, _key=_key)
            for item in value
        ]
    if _key.casefold() not in _URL_PAYLOAD_KEYS or not isinstance(value, str):
        return value
    parsed = urlsplit(value)
    if (
        parsed.scheme in {"http", "https"}
        and parsed.hostname
        and parsed.hostname.casefold() == (source.hostname or "").casefold()
        and parsed.port == source.port
    ):
        netloc = replacement.netloc
        return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    return value


def _worker_flag_pattern(environment: Mapping[str, Any] | None = None) -> str:
    """Return the challenge flag shape seeded through the Worker environment.

    The Coordinator publishes it as ``MUTEKI_FLAG_PATTERN`` so the Worker
    prompt, the official gate and the container-side blackboard script all
    agree on the same banner instead of each hardcoding ``flag{...}``.
    """

    pattern = str((environment or {}).get("MUTEKI_FLAG_PATTERN") or "").strip()
    return pattern or r"flag\{.*?\}"


def _safe_environment(values: Mapping[str, Any]) -> dict[str, str]:
    allowed_prefixes = (
        "MUTEKI_",
        "CODEX_",
        "CLAUDE_",
        "CURSOR_",
        "OPENAI_",
        "ANTHROPIC_",
        # IS_SANDBOX is an exact name, not a prefix, but it must survive this
        # filter for the reason spelled out in ``_claude_root_permission_env``:
        # the Claude CLI refuses to run as root without it, and a filtered-out
        # variable looks exactly like one that was never set.
        "IS_SANDBOX",
    )
    return {
        str(key): str(value)
        for key, value in values.items()
        if str(key).startswith(allowed_prefixes) and value is not None
    }


#: What the Claude CLI prints, and refuses to run, when it detects root:
#:
#:     --dangerously-skip-permissions cannot be used with root/sudo privileges
#:
#: The check is on ``geteuid() == 0``, and the only documented escape is the
#: ``IS_SANDBOX`` environment variable.  Our worker image runs as ``uid=0``, so
#: *every* containerised Claude turn hit this -- probe and real turn alike.
_CLAUDE_ROOT_REFUSAL = "cannot be used with root/sudo privileges"


def _claude_root_permission_env(engine: str, environment: dict[str, str]) -> None:
    """Set ``IS_SANDBOX=1`` for a containerised Claude run as root.

    Measured 2026-10-25 in ``muteki-run-9911bf0d``: the PREPARE health check for
    ``claude-cli:7e58443e`` failed with ``WORKER_HEALTHCHECK_FAILED`` on every
    Run, the Coordinator logged ``ENGINE_UNAVAILABLE``, and the Run fell back to
    a single Codex lane.  Reproduced by hand inside the same container:

        $ claude -p --dangerously-skip-permissions -- "Reply OK."
        --dangerously-skip-permissions cannot be used with root/sudo privileges

    The engine was healthy the whole time -- the host-side config test passed in
    3.2s against the same relay.  Only the CLI's own root guard was in the way,
    and because the guard fires *before* any network call it was misread as a
    dead endpoint for several rounds.

    ``IS_SANDBOX`` is the CLI's own opt-out and means exactly what it says: the
    caller asserts the sandbox is the isolation boundary.  That is true here --
    the Worker already runs inside a dedicated container -- so setting it does
    not weaken a boundary that exists, it tells the CLI where the boundary is.

    **The caller passes ``profile.engine_id``**, not a driver name.  That value
    looks like ``claude-cli:7e58443e-...``, so an equality test against
    ``"claude"`` never fires -- which is exactly how the first version of this
    fix shipped inert and cost another two hours.  Match on the prefix instead,
    and keep accepting a bare ``claude`` for callers that do pass a driver name.
    """
    name = str(engine or "").strip().casefold()
    if not name:
        return
    # ``claude-cli:<config-id>`` is the real spelling; ``claude`` is what a
    # driver-name caller passes.  ``claude_code``/``claude-code`` also occur in
    # older profile dumps.  None of them may collide with codex/cursor.
    if not (name == "claude" or name.startswith("claude-cli") or name.startswith("claude_code")
            or name.startswith("claude-code")):
        return
    environment.setdefault("IS_SANDBOX", "1")


#: The docker host alias the proxies publish to a Worker container.
_PROXY_HOST_ALIAS = "host.docker.internal"


def _pin_host_alias_to_ipv4(container: str) -> bool:
    """Drop the IPv6 row for ``host.docker.internal`` inside the Worker container.

    Measured 2026-10-26 (runs ``6457e8dc`` and neighbours): **every** Codex lane
    died at ``WORKER_HEALTHCHECK_FAILED`` while Claude on the same container was
    healthy, and the host-side config probe answered in 3s.

    ``ensure_container`` starts the Worker with::

        --add-host host.docker.internal:host-gateway

    On Docker Desktop that injects **two** rows::

        192.168.65.254      host.docker.internal
        fdc4:f303:9324::254 host.docker.internal

    Both proxy launchers bind ``0.0.0.0``, which is IPv4-only.  So the alias
    resolves to an IPv6 address nothing is listening on.  curl and Node (Claude)
    fall back to IPv4 and connect; **Codex is a Rust binary using reqwest's
    Happy Eyeballs, which takes the first resolved address and does not retry on
    the other family**, so it dials the dead IPv6 address and fails before any
    request is sent.

    That asymmetry is the whole shape of the bug: one lane healthy, one lane
    dead, same config, same container, same relay.

    Stripping the IPv6 row is the narrow fix.  Rewriting the launcher to bind
    ``::`` is not an option -- on Windows ``::`` is not dual-stack
    (``IPV6_V6ONLY`` defaults to 1, measured), so that would trade one dead
    family for the other.
    """

    name = str(container or "").strip()
    if not name:
        return False
    alias = _PROXY_HOST_ALIAS
    script = (
        "f=/etc/hosts\n"
        "[ -w \"$f\" ] || exit 0\n"
        # Keep every line except the ones that map the alias to an IPv6 address.
        # A `host.docker.internal` entry whose first field contains ':' is IPv6,
        # because IPv4 literals never do.  The IPv4 row (when present) survives.
        f"grep -v -E '^[^[:space:]]*:[^[:space:]]*[[:space:]]+{alias}([[:space:]]|$)' \"$f\" > \"$f.new\" || true\n"
        # If no IPv4 mapping survived, add one derived from the container's own
        # resolver so the alias stays usable rather than silently unresolvable.
        f"if ! grep -q -E '^[0-9.]+[[:space:]]+{alias}([[:space:]]|$)' \"$f.new\"; then\n"
        f"  ip4=$(getent ahostsv4 {alias} 2>/dev/null | head -1 | cut -d' ' -f1)\n"
        f"  [ -n \"$ip4\" ] && printf '%s %s\\n' \"$ip4\" '{alias}' >> \"$f.new\"\n"
        "fi\n"
        "cat \"$f.new\" > \"$f\"\n"
        "rm -f \"$f.new\"\n"
        f"getent hosts {alias} | head -1"
    )
    try:
        completed = subprocess.run(
            ["docker", "exec", name, "sh", "-c", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


#: Health-probe failures worth retrying.  Deliberately narrow: a timeout or a
#: dropped transport can be a saturated gateway, but a non-zero exit from a
#: missing binary or a rejected credential will fail identically on the second
#: try, so retrying those would only spend the Run's budget twice.
_TRANSIENT_HEALTH_REASONS: frozenset[str] = frozenset(
    {
        "WORKER_HEALTHCHECK_TIMEOUT",
        "WORKER_HEALTHCHECK_FAILED",
    }
)

#: Pause before the single retry.  Long enough to step out of a burst of
#: upstream 503s, short enough not to matter against the Run budget.
_HEALTH_RETRY_DELAY_SECONDS = 8.0


def _health_failure_is_transient(reason: str) -> bool:
    return str(reason or "") in _TRANSIENT_HEALTH_REASONS


def _parse_challenge_brief(raw: Any) -> list[str]:
    """Decode the Challenge's declared surface carried on the Worker profile.

    A decode failure must not stop a Worker from running: an unreadable brief
    degrades to "no brief", which is exactly the behaviour before this channel
    existed, while raising here would turn a cosmetic prompt field into a lost
    Worker turn.
    """

    text = str(raw or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()][:24]


def _log_health_failure(engine: str, reason: str, result: Any) -> str:
    """把体检失败的**原始输出**写进日志，再把稳定的原因码原样返回。

    ``reason_code`` 必须是稳定枚举——UI、审计流和调度都读它，所以失败细节
    不能塞进返回值。但它总得留在某处：为了回答「codex 为什么体检失败」，
    这一轮翻了几个小时的 ``run_events``，而事件里只有一个常量，容器又往往
    在结论落下时已经销毁，证据跟着一起没了。

    所以失败时留一条带 rc / stdout / stderr 的 warning。它是排查的唯一入口。
    """

    if reason and reason != "HEALTHY":
        runtime_status = getattr(result, "runtime_status", None) or {}
        returncode = runtime_status.get("rc") if isinstance(runtime_status, Mapping) else None
        logger.warning(
            "Worker health probe failed for %s: %s rc=%s timed_out=%s "
            "stdout=%r stderr=%r",
            engine,
            reason,
            returncode,
            bool(getattr(result, "timed_out", False)),
            str(getattr(result, "text", "") or "").strip()[-300:],
            str(getattr(result, "raw_stderr", "") or "").strip()[-600:],
        )
    return reason


def _probe_answered_before_hangup(result: Any, returncode: object) -> bool:
    """Return True when the health probe printed its answer but was reaped with SIGHUP.

    The probe prompt is ``Reply exactly OK. Do not use tools.``, so an ``OK`` on
    stdout is the success signal itself.  Measured 2026-10-26 (backend log, one
    cycle): **all 5** probe failures in that cycle were
    ``rc=-1 timed_out=False stdout='OK'`` -- the CLI answered and was then hung
    up while the container-side supervisor tore the turn down, which surfaces as
    ``rc == -1`` (SIGHUP).  Failing the engine on that signal throws away an
    engine that had just proved it could answer, and the cost is not cosmetic:
    across 309 Runs, 184 reached two lanes and 94 fell to one, and a single dead
    lane loses the Run.

    The test is deliberately narrow:

    * only ``-1`` qualifies -- ``rc=1``/``127`` still mean a broken launch and
      are rejected by ``_classify_cli_health_failure`` (see
      ``test_official_worker_health_rejects_nonzero_upstream_cli_result``);
    * stdout must be exactly the expected answer, so an error banner that merely
      contains the letters ``ok`` does not pass;
    * stderr must carry nothing except the fixed transport notices in
      :data:`_BENIGN_STDERR_NOTICES`, so a CLI that warns or crashes after
      answering is still reported as unhealthy.
    """

    try:
        if int(returncode) != -1:
            return False
    except (TypeError, ValueError):
        return False
    answer = str(getattr(result, "text", "") or "").strip().casefold()
    stderr = str(getattr(result, "raw_stderr", "") or "").strip()
    return answer == "ok" and not _unexpected_stderr(stderr)


# codex prints these to stderr when the prompt arrives on stdin rather than in
# argv.  They describe the transport, not the answer: measured 2026-10-28,
# every codex probe failure after the relay balance was restored was
# ``rc=-1 stdout='OK' stderr='Reading additional input from stdin...'`` -- the
# engine had answered correctly and only the teardown signal was odd.  Rejecting
# that pattern costs the codex lane on every Run, so the notice is ignored.
# Anything else on stderr still means the CLI complained after answering.
_BENIGN_STDERR_NOTICES = frozenset(
    {
        "Reading additional input from stdin...",
    }
)


def _unexpected_stderr(stderr: str) -> list[str]:
    """Return the stderr lines that are not known-benign transport notices."""

    return [
        line.strip()
        for line in (stderr or "").splitlines()
        if line.strip() and line.strip() not in _BENIGN_STDERR_NOTICES
    ]


def _drop_stale_codex_oauth_home(environment: dict[str, str]) -> None:
    """Keep codex on the injected API key instead of a stale OAuth home.

    ``runtime_env_for_engine`` sets ``CODEX_HOME`` whenever the account store has
    a ``codex-home`` directory.  It decides that from the account files alone
    (its ``out`` dict starts empty and never inherits the caller's ``env``), so
    it cannot see the ``OPENAI_API_KEY`` that ``muteki_runtime`` already put in
    the environment -- the local Responses proxy's token.  The two then collide:
    codex prefers ``auth.json`` over ``env_key``, refreshes the expired token at
    startup, and that refresh is rejected from this host.  Measured 2026-10-27
    (three of twelve Runs): the probe answered ``OK`` and the process still died
    with ``rc=-1`` and
    ``stderr='... to refresh token: 403 Forbidden: {"error":{"code":
    "unsupported_country_region_territory"...``.  That reads as a dead engine,
    so those Runs fell to a single lane.

    With a key in the environment codex needs no OAuth home at all -- measured in
    the worker image: empty ``CODEX_HOME`` plus ``OPENAI_API_KEY=dummy`` sends the
    request straight to the configured ``base_url`` and never touches the auth
    endpoint.  Only the variable is dropped; the account's files are untouched,
    and an account that genuinely authenticates by OAuth (no key anywhere) keeps
    its ``CODEX_HOME``.
    """

    if environment.get("OPENAI_API_KEY") or environment.get("OPENAI_API_KEY_FILE"):
        environment.pop("CODEX_HOME", None)


def _classify_cli_health_failure(result: Any) -> str:
    """Return a safe, stable reason for a failed CLI health probe.

    The response body is deliberately not persisted or returned.  This only
    classifies the small set of operator-actionable failures that otherwise
    all appeared as ``WORKER_HEALTHCHECK_FAILED`` in the UI and audit stream.

    Cursor's two failure modes are included because they are indistinguishable
    from a dead container otherwise: ``Failed to reach the Cursor API`` means the
    configured endpoint is reachable but does not speak the Cursor protocol (so
    no key will ever help), while ``The provided API key is invalid`` means the
    relay IS protocol-compatible and only the credential is wrong.  Those need
    opposite operator responses, so they must not collapse into one code.
    """

    text = " ".join(
        str(getattr(result, name, "") or "")
        for name in ("text", "raw_stderr")
    ).casefold()
    if "failed to reach the cursor api" in text:
        return "CURSOR_CLI_ENDPOINT_UNREACHABLE"
    if "provided api key is invalid" in text:
        return "CURSOR_CLI_AUTH_FAILED"
    if "requires a newer version of codex" in text:
        return "CODEX_CLI_VERSION_UNSUPPORTED"
    if (
        "401 unauthorized" in text
        or "missing bearer" in text
        or "authentication required" in text
        or "not logged in" in text
    ):
        return "CODEX_CLI_AUTH_FAILED"
    return "WORKER_HEALTHCHECK_FAILED"


def _credential_engine_name(
    engine_id: str,
    driver_profile: Mapping[str, Any] | None,
) -> str:
    """Map a stable Worker identity to its credential transport name.

    ``codex-cli:<config-id>`` is an audit/scheduling identity, not a
    credential-store key. Muteki profiles keep the executable transport in
    ``engine``; use that value for account projection while preserving the
    full identity everywhere else.
    """

    profile_engine = str((driver_profile or {}).get("engine") or "").strip()
    return profile_engine or str(engine_id or "").strip()


def _containerize_environment(values: Mapping[str, str], handle: Any | None) -> dict[str, str]:
    """Map solver paths from the host workspace into the Worker container.

    ``WorkerJob`` is assembled by the host-side Coordinator, so its Blackboard
    and workspace variables are host paths.  The container only sees the run
    workspace mounted at ``/home/kali/workspace``.  Keep this translation at
    the execution boundary instead of leaking container paths into Graph or
    Evidence code, and only translate the two solver-owned path variables.
    """

    projected = dict(values)
    if handle is not None:
        projected.setdefault("MUTEKI_BLACKBOARD_SCRIPT", "/usr/local/bin/blackboard.py")
    to_container_path = getattr(handle, "to_container_path", None)
    for key in ("MUTEKI_WORKSPACE", "MUTEKI_BLACKBOARD_DB"):
        value = projected.get(key)
        if not value:
            continue
        # A value that is ALREADY a container path still needs its separators
        # normalised: the container is Linux, where ``graph\x.db`` is a single
        # file name, not a directory.  This used to be skipped as "already done"
        # (``continue`` on the prefix), which is exactly how the Worker ended up
        # with a shadow blackboard database and ``no such table: events``.
        normalised = str(value).replace("\\", "/")
        if normalised == _CONTAINER_WORKSPACE or normalised.startswith(
            _CONTAINER_WORKSPACE + "/"
        ):
            projected[key] = normalised
            continue
        if not callable(to_container_path):
            continue
        try:
            projected[key] = str(to_container_path(value)).replace("\\", "/")
        except (OSError, TypeError, ValueError):
            # The Worker will receive the original path and report the normal
            # execution failure; do not make an unrelated adapter exception
            # escape the Worker boundary.
            continue
    return projected


def _bridge_failure_metadata(result: OfficialWorkerResult, reason: str) -> dict[str, Any]:
    """Keep only existing bounded execution metadata on a bridge failure."""

    metadata = dict(result.metadata)
    metadata["evidence_bridge"] = "failed"
    metadata["evidence_bridge_reason"] = reason
    return metadata


def _largest_artifact_path(root: Path) -> str:
    """Return the largest new official artifact as a path-only handoff.

    The official Worker stores the full transcript and marker witnesses in its
    ArtifactStore.  The largest artifact is the bounded whole-run transcript;
    the Evidence bridge may copy it into the application's existing Evidence
    chain, while the path itself never becomes solver state.
    """

    try:
        candidates = [path for path in root.glob("*") if path.is_file()]
        if not candidates:
            return ""
        return str(max(candidates, key=lambda path: path.stat().st_size).resolve())
    except (OSError, ValueError):
        return ""


__all__ = [
    "OfficialWorkerAdapter",
    "OfficialWorkerConfig",
    "OfficialWorkerEvidenceBridge",
    "OfficialWorkerResult",
    "_container_target_url",
    "_rewrite_target_urls",
]
