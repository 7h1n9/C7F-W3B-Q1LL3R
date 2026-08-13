"""Adapter for the vendored Muteki CLI/Sandbox Worker.

Production Muteki runs use the upstream one-container-per-run Sandbox/Control
Plane.  Legacy ToolGateway/Kali Runner execution is outside this adapter and
is not a fallback after a native Worker failure.  Raw CLI output never becomes
an audit event; callers must turn confirmed output into Evidence before
completion.
"""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import json
import os
import threading
import time
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit, urlunsplit

from ..adapter.official_evidence import (
    OfficialWorkerEvidenceBridge,
    sanitize_evidence_refs,
)
from ..outcomes import DeadEndSignal


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

    async def start(
        self,
        *,
        run_id: str,
        workspace: str,
        account_root: str | None = None,
    ) -> None:
        """Start the official one-container-per-run Sandbox when requested."""

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
        # `ensure_container` may replace the operator's account store with a
        # readable per-run projection.  Resolve credential paths against that
        # projection so the container receives only CODEX_HOME/CREDENTIAL_FILE
        # references, never the host secret contents.
        self._account_root = str(
            getattr(self._container, "account_root", None) or account_root or ""
        ) or None

    async def stop(self) -> None:
        """Tear down the run Sandbox and clear the local handle."""

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
        if not result.success or self.evidence_bridge is None:
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
        """

        if self._container is None:
            return False, "WORKER_SANDBOX_NOT_READY"
        from muteki.solver.cli_driver import driver_for
        from muteki.solver.container_exec import run_cli_container

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
            except Exception:
                return False, "CREDENTIAL_RESOLUTION_FAILED"
        argv = driver.build_execute(
            "Reply exactly OK. Do not use tools.",
            None,
            web_access=False,
            kb_access=False,
            stream=False,
        )
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
        except Exception:
            return False, "WORKER_HEALTHCHECK_FAILED"
        if result.timed_out:
            return False, "WORKER_HEALTHCHECK_TIMEOUT"

        # The official RCP path returns Muteki's ``CliResult``.  It has no
        # ``success`` attribute; success is represented by a finished runtime
        # status and a non-error process return code.  Keep compatibility with
        # the older test/container adapter result without making the upstream
        # object pretend to be a ContainerResult.
        if bool(getattr(result, "oom_killed", False)):
            return False, "WORKER_HEALTHCHECK_FAILED"
        if bool(getattr(result, "cancelled", False)):
            return False, "WORKER_HEALTHCHECK_FAILED"
        runtime_status = getattr(result, "runtime_status", None) or {}
        returncode = runtime_status.get("rc") if isinstance(runtime_status, Mapping) else None
        if returncode is not None and int(returncode) != 0:
            return False, "WORKER_HEALTHCHECK_FAILED"
        if hasattr(result, "success") and not bool(getattr(result, "success")):
            return False, "WORKER_HEALTHCHECK_FAILED"
        if not hasattr(result, "success") and not str(getattr(result, "text", "") or "").strip():
            return False, "WORKER_HEALTHCHECK_FAILED"
        return True, ""

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
        prompt = _build_prompt(goal, intent_id, payload)
        driver = driver_for(
            dict(driver_profile)
            if isinstance(driver_profile, Mapping) and driver_profile
            else engine
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
            except Exception:
                return OfficialWorkerResult(
                    False,
                    "FAILED",
                    engine,
                    metadata={"reason": "CREDENTIAL_RESOLUTION_FAILED"},
                )
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
            flag_gate_instruction = _flag_gate_instruction(payload)
            if flag_gate_instruction:
                intent_goal += "\n" + flag_gate_instruction
        challenge = UpstreamChallenge(
            id=str(getattr(job, "challenge_id", "") or "native-challenge"),
            name=goal[:160] or "Native Muteki action",
            category="web",
            description="Execute one bounded Solver Intent against the declared target.",
            target=target,
            flag_format=r"flag\{.*?\}",
        )
        if self._account_root:
            try:
                from muteki.solver.credential_accounts import runtime_env_for_engine

                credential_env = runtime_env_for_engine(
                    engine,
                    account_root=self._account_root,
                    container=self.config.backend == "container",
                    env=environment,
                ).env
                environment.update(_safe_environment(credential_env))
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
        raw_driver_profile = getattr(job, "driver_profile", None)
        driver_profile = (
            dict(raw_driver_profile)
            if isinstance(raw_driver_profile, Mapping) and raw_driver_profile
            else None
        )
        solver = CliSolver(
            spec=SimpleNamespace(solver_id=str(getattr(job, "worker_id", "native-worker"))),
            challenge=challenge,
            shared_graph=self.shared_graph,
            driver=driver_for(
                dict(driver_profile)
                if isinstance(driver_profile, Mapping) and driver_profile
                else engine
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

    def _on_step(self, job: Any, step: Any) -> None:
        if self.step_callback is None:
            return
        # Only identity/progress metadata is exposed.  Tool output and model
        # text may contain credentials or target data and stay inside the worker.
        self.step_callback(
            {
                "worker_id": str(getattr(job, "worker_id", "")),
                "kind": str(getattr(step, "kind", "")),
                "tool": str(getattr(step, "tool", "")),
                "session": str(getattr(step, "session", "")),
            }
        )


def _build_prompt(goal: str, intent_id: str, payload: dict[str, Any]) -> str:
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
            flag_gate_instruction = _flag_gate_instruction(payload)
            if flag_gate_instruction:
                lines.append(flag_gate_instruction)
    return "\n".join(lines)


def _flag_gate_instruction(payload: Mapping[str, Any]) -> str:
    """Return the native-only completion instruction for flag extraction.

    The extracted value stays inside the official Worker and its Blackboard
    gate.  It must never be copied into the typed observation consumed by the
    host-side Strategy adapter.
    """

    tool_name = str(payload.get("tool_name") or payload.get("tool") or "").strip()
    if tool_name != "boolean_config_extract":
        return ""
    return (
        "If the bounded extraction obtains a flag-shaped candidate, use the "
        "official Blackboard `write-flag` command with the exact candidate and "
        "the real captured output so the official provenance gate decides it. "
        "Never place the candidate, raw output, or any secret in "
        "OBSERVATION_JSON or VERIFIED_FACT; those typed fields are only the "
        "bounded extraction status."
    )


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


def _safe_environment(values: Mapping[str, Any]) -> dict[str, str]:
    allowed_prefixes = ("MUTEKI_", "CODEX_", "CLAUDE_", "CURSOR_", "OPENAI_", "ANTHROPIC_")
    return {
        str(key): str(value)
        for key, value in values.items()
        if str(key).startswith(allowed_prefixes) and value is not None
    }


def _containerize_environment(values: Mapping[str, str], handle: Any | None) -> dict[str, str]:
    """Map solver paths from the host workspace into the Worker container.

    ``WorkerJob`` is assembled by the host-side Coordinator, so its Blackboard
    and workspace variables are host paths.  The container only sees the run
    workspace mounted at ``/home/kali/workspace``.  Keep this translation at
    the execution boundary instead of leaking container paths into Graph or
    Evidence code, and only translate the two solver-owned path variables.
    """

    projected = dict(values)
    if handle is None:
        return projected
    to_container_path = getattr(handle, "to_container_path", None)
    if not callable(to_container_path):
        return projected
    projected.setdefault("MUTEKI_BLACKBOARD_SCRIPT", "/usr/local/bin/blackboard.py")
    for key in ("MUTEKI_WORKSPACE", "MUTEKI_BLACKBOARD_DB"):
        value = projected.get(key)
        if not value or value.startswith("/home/kali/workspace"):
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
