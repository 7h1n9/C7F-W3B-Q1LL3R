from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.database import SessionLocal
from app.engines.openai_compatible import OpenAICompatibleEngine
from app.models.challenge import Challenge
from app.models.model_config import ModelConfig
from app.models.run import SolveRun
from app.schemas.challenge import normalize_asset_warranty_metadata
from app.services.crypto import decrypt_api_key
from app.solver.muteki.adapter import (
    EventBridge,
    SqlAlchemyOfficialEvidenceBridge,
)
from app.solver.muteki.adapter.cost_bridge import (
    record_muteki_reason_usage,
    record_official_worker_usage,
)
from app.solver.muteki.adapter.reason_model import CoordinatorReasonModel
from app.solver.muteki.adapter.upstream_runtime_graph import (
    create_runtime_graph,
    runtime_graph_backend,
)
from app.solver.muteki.core.orchestrator import MutekiOrchestrator, MutekiRunResult
from app.solver.muteki.insight_bus import Insight, InsightBus, InsightKind
from app.solver.muteki.events import EventType
from app.solver.muteki.graph import MutekiGraph
from app.solver.muteki.reason import MutekiReason
from app.solver.muteki.recon.breadth_scanner import _public_demo_credentials
from app.solver.muteki.recon.fingerprint import classify_challenge
from app.solver.muteki.runtime.configuration import runtime_selection_from_hints
from app.solver.muteki.strategy import MutekiStrategyPlanner
from app.solver.muteki.workers import EngineProfile, WorkerJob, WorkerOutcome

MUTEKI_CONTAINER_ONLY_BACKEND = "upstream_container"


def resolve_muteki_worker_backend(configured_backend: str | None = None) -> str:
    """Resolve the only valid production Muteki Worker boundary.

    Legacy Gateway/Kali Runner and host-local CLI paths remain available to
    legacy solver modes and focused Coordinator tests. They are not valid for
    a production ``solver_mode=muteki`` Run, so configuration drift fails
    closed instead of silently crossing the old VM boundary.
    """

    value = (
        os.environ.get("APP_MUTEKI_WORKER_BACKEND", "")
        if configured_backend is None
        else configured_backend
    )
    normalized = str(value or "").strip().casefold()
    if normalized in {"", MUTEKI_CONTAINER_ONLY_BACKEND}:
        return MUTEKI_CONTAINER_ONLY_BACKEND
    if normalized in {"upstream_local", "local"}:
        return "upstream_local"
    raise ValueError(
        "MUTEKI_CONTAINER_ONLY: production Muteki requires "
        "APP_MUTEKI_WORKER_BACKEND=upstream_container; "
        f"received {normalized!r}"
    )


def _resolve_official_account_root(
    workspace_path: str,
    *,
    explicit_root: str | None = None,
) -> str | None:
    """Resolve the standard Muteki account store for a production Run.

    The official container contract projects credentials from the durable
    ``sessions/_secrets/accounts`` store.  The old adapter only consulted an
    environment variable, so an account imported through the Settings page
    was silently ignored by production Runs.  An explicit environment value
    remains authoritative; otherwise derive the sibling ``sessions`` folder
    from the Run workspace (``data/workspaces/<run-id>``).
    """

    if explicit_root and str(explicit_root).strip():
        return str(Path(explicit_root).expanduser().resolve())
    workspace = Path(workspace_path).expanduser().resolve()
    data_root = workspace.parent.parent
    account_root = data_root / "sessions" / "_secrets" / "accounts"
    if account_root.is_dir():
        return str(account_root)
    return None


@dataclass(frozen=True, slots=True)
class _ChallengeSnapshot:
    """Immutable challenge data used after ToolGateway commits the session."""

    id: str
    name: str
    description: str
    challenge_type: str
    target_url: str | None
    allowed_hosts: list[str]
    flag_pattern: str
    source_path: str | None
    metadata_json: dict[str, Any]


class MutekiRuntime:
    """Build one isolated canonical runtime for an existing SolveRun."""

    def __init__(self, session: Any, run: SolveRun, challenge: Challenge) -> None:
        self.session = session
        self.run = run
        # ToolGateway commits between worker turns.  Keep immutable runtime
        # identifiers out of expired ORM attribute access; the Blackboard and
        # Evidence adapters only need these scalar values.
        self._run_id = str(run.id)
        self._workspace_path = str(run.workspace_path)
        # Gateway commits happen between Solver turns.  Keep Runtime reads
        # detached from the SQLAlchemy Challenge instance so the next Reason
        # pass cannot trigger an async lazy load (MissingGreenlet).
        # API serialization already applies the Challenge schema's metadata
        # normalization.  The production Solver must consume that same
        # contract from the ORM boundary; otherwise adapter-backed challenges
        # lose their declared fields and silently fall back to ``query``.
        normalized_metadata = normalize_asset_warranty_metadata(challenge)
        self.challenge = _ChallengeSnapshot(
            id=str(challenge.id),
            name=str(challenge.name or ""),
            description=str(challenge.description or ""),
            challenge_type=str(challenge.challenge_type or "WEB_TARGET"),
            target_url=str(challenge.target_url) if challenge.target_url else None,
            allowed_hosts=[str(item) for item in (challenge.allowed_hosts or [])],
            flag_pattern=str(challenge.flag_pattern or r"flag\{[^}]+\}"),
            source_path=str(challenge.source_path) if challenge.source_path else None,
            metadata_json=dict(normalized_metadata or {}),
        )
        self.event_bridge: EventBridge | None = None
        self._graph: MutekiGraph | None = None
        self._insight_bus: InsightBus | None = None
        self._public_credentials: tuple[str, str] | None = None
        self._reason_model: CoordinatorReasonModel | None = None
        self._worker_models: dict[str, OpenAICompatibleEngine] = {}

    async def run_once(self, *, max_rounds: int | None = None, max_workers: int = 10) -> MutekiRunResult:
        """Run one production Muteki session in its run-scoped container.

        ``max_rounds`` remains an explicit compatibility/test override.  The
        normal production path leaves it unset so Coordinator progress is
        driven by SharedGraph changes and the persisted Run total-runtime
        limit, not by the unrelated Agent-step budget.
        """

        root = Path(self._workspace_path).resolve() / "muteki"
        # Production Muteki uses the upstream SharedGraph by default.  The
        # compatibility graph remains available through an explicit
        # APP_MUTEKI_GRAPH_BACKEND=current override for tests/legacy rollout.
        graph_backend = runtime_graph_backend(default="upstream")
        graph_path = root / "graph" / (
            "upstream_shared_graph.db" if graph_backend == "upstream" else "shared_graph.db"
        )
        # Event persistence uses a short-lived independent session because
        # graph callbacks can be scheduled while the worker session is busy
        # collecting ToolGateway results.
        self.event_bridge = EventBridge(SessionLocal, run_id=self._run_id)
        # Per-run worker Insight Bus: verified facts / dead-ends / flags are
        # fanned out to every participating worker's inbox in real time so a
        # Codex worker immediately sees what an OpenAI worker confirmed without
        # waiting for the next Coordinator poll.  The bus is memory-only and
        # per-Run, matching upstream Muteki's per-challenge fan-out hub.
        self._insight_bus = InsightBus(challenge_id=self._run_id)

        def _graph_to_insights(event) -> None:
            # The graph already fans out to the audit EventBridge; mirror the
            # same ordering here so the InsightBus is never a source of truth,
            # just a low-latency collaboration channel for active workers.
            try:
                subscriber = self.event_bridge.callback()
                if subscriber is not None and callable(subscriber):
                    subscriber(event)
            except Exception:
                if self._graph is not None:
                    with contextlib.suppress(Exception):
                        self._graph.add_dead_end(
                            actor="muteki-runtime",
                            description="EVENT_BRIDGE_CALLBACK_FAILED",
                        )
            if self._insight_bus is None:
                return
            etype = str(getattr(event, "event_type", "") or "")
            payload = dict(getattr(event, "payload", {}) or {})
            actor = str(getattr(event, "actor", "") or "worker")
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if etype == "fact_added" or etype == EventType.FACT_ADDED:
                text = str(payload.get("content") or payload.get("fact") or "")
                verified = bool(getattr(event, "verified", False))
                if text and verified and loop is not None:
                    try:
                        self._graph_to_insight_task = loop.create_task(
                            self._insight_bus.fact(actor, text),
                            name=f"muteki-insight-fact-{self._run_id}",
                        )
                    except Exception:
                        pass
            elif etype == "dead_end" or etype == EventType.DEAD_END:
                text = str(payload.get("description") or payload.get("reason") or "")
                if text and loop is not None:
                    try:
                        self._graph_to_insight_task = loop.create_task(
                            self._insight_bus.dead_end(actor, text),
                            name=f"muteki-insight-deadend-{self._run_id}",
                        )
                    except Exception:
                        pass
            elif etype == "flag_found" or etype == EventType.FLAG_FOUND:
                flag = str(payload.get("flag") or "")
                if flag and loop is not None:
                    try:
                        self._graph_to_insight_task = loop.create_task(
                            self._insight_bus.flag_found(actor, flag),
                            name=f"muteki-insight-flag-{self._run_id}",
                        )
                    except Exception:
                        pass

        self._graph = create_runtime_graph(
            graph_path,
            challenge=self.challenge,
            challenge_id=self._run_id,
            event_subscriber=_graph_to_insights,
            backend=graph_backend,
        )
        reason_provider = await self._build_reason_provider()
        reason = MutekiReason(provider=reason_provider, metadata=self.challenge.metadata_json or {})
        worker_backend = resolve_muteki_worker_backend()
        native_evidence_bridge = SqlAlchemyOfficialEvidenceBridge(
            self.session,
            self.run,
            self._workspace_path,
        )
        official_account_root = _resolve_official_account_root(
            self._workspace_path,
            explicit_root=os.environ.get("MUTEKI_ACCOUNT_ROOT"),
        )
        try:
            engine_profiles = await self._worker_profiles(worker_backend)
            # Map the existing Run limits onto the official Muteki boundaries:
            # one native Worker is one durable application/tool turn, while
            # its internal CLI loop keeps the separate upstream ``max_turns``
            # limit.  Capping the smaller of the two Run budgets prevents a
            # barren route from spawning indefinitely, without putting the
            # legacy Agent/ToolGateway counters inside the official Worker.
            max_agent_steps = max(1, int(getattr(self.run, "max_agent_steps", 120) or 120))
            max_tool_calls = max(1, int(getattr(self.run, "max_tool_calls", 120) or 120))
            native_worker_budget = min(max_agent_steps, max_tool_calls)
            orchestrator = MutekiOrchestrator(
                self._graph,
                reason,
                # The callback is deliberately fail-closed. Production
                # Muteki workers are routed by WorkerPool to the official
                # Sandbox container; no callback may re-enter ToolGateway or
                # the legacy Kali Runner.
                worker_runner=self._disabled_compat_worker,
                engines=engine_profiles,
                max_workers=max_workers,
                worker_backend=worker_backend,
                official_worker_evidence_bridge=native_evidence_bridge,
                official_worker_usage_bridge=self._record_official_worker_usage,
                official_account_root=official_account_root,
                # Match upstream Swarm's bounded Race/Explore window.  This
                # leaves the outer total budget available for Coordinator
                # recovery and subsequent Reason/Worker turns after one
                # Worker is reclaimed.
                worker_timeout_seconds=max(
                    1,
                    min(
                        720,
                        int(getattr(self.run, "max_runtime_seconds", 900) or 900),
                    ),
                ),
                worker_max_turns=min(80, max_agent_steps),
                max_total_workers=native_worker_budget,
                insight_bus=self._insight_bus,
            )
            total_timeout = max(
                1,
                int(
                    getattr(self.run, "max_total_runtime_seconds", 0)
                    or max(
                        60,
                        (max_rounds or 1)
                        * max(1, int(getattr(self.run, "max_runtime_seconds", 900) or 900)),
                    ),
                ),
            )
            result = await self._run_orchestrator_with_deadline(
                orchestrator,
                max_rounds=max_rounds,
                total_timeout=total_timeout,
            )
            # \u00a716 distillation: on a successful solve, distill the
            # solved graph into a reusable template and store it.
            if result.flag_found and result.graph_path:
                self._distill(result.graph_path, reason=str(result.reason or ""))

            try:
                await asyncio.wait_for(self.event_bridge.flush(), timeout=15.0)
            except asyncio.TimeoutError:
                self._graph.add_dead_end(actor="muteki-runtime", description="EVENT_BRIDGE_FLUSH_TIMEOUT")
            return result
        finally:
            # Graph callbacks are intentionally persisted through an ordered
            # independent-session EventBridge.  A cancelled Run must still
            # drain that tail before the graph/model handles close, otherwise
            # the final worker/recovery audit events disappear on restart.
            if self.event_bridge is not None:
                with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                    await asyncio.wait_for(
                        asyncio.shield(self.event_bridge.flush()),
                        timeout=15.0,
                    )
            if self._reason_model is not None:
                await self._reason_model.close()
                self._reason_model = None
            for model in tuple(self._worker_models.values()):
                await model.close()
            self._worker_models.clear()
            if self._insight_bus is not None:
                for sid in tuple(self._insight_bus._inboxes):
                    self._insight_bus.unsubscribe(sid)
            if self._graph is not None:
                self._graph.close()

    async def _run_orchestrator_with_deadline(
        self,
        orchestrator: MutekiOrchestrator,
        *,
        max_rounds: int | None,
        total_timeout: int,
    ) -> MutekiRunResult:
        """Run the Solver Loop under the total deadline.

        Reaching the deadline is a cooperative stop request, not a hard task
        cancellation.  The Coordinator owns its terminal ``Finalize`` step in
        the normal return path, so Runtime must await that path to completion
        before reporting the timed-out Run.
        """

        run_task = asyncio.create_task(
            orchestrator.run(max_rounds=max_rounds),
            name=f"muteki-orchestrator-{self._run_id}",
        )
        try:
            return await asyncio.wait_for(
                asyncio.shield(run_task),
                timeout=total_timeout,
            )
        except asyncio.TimeoutError:
            if self._graph is not None:
                self._graph.add_dead_end(
                    actor="muteki-runtime",
                    description="MUTEKI_RUN_TIMEOUT",
                )
            request_stop = getattr(
                getattr(orchestrator, "coordinator", None),
                "request_stop",
                None,
            )
            if callable(request_stop):
                request_stop("MUTEKI_RUN_TIMEOUT")
            # Do not cancel here.  Coordinator.run owns Finalize in its
            # ``finally`` path; cancelling can interrupt asynchronous cleanup
            # and leaves the terminal Blackboard handoff incomplete.
            return await run_task
        except BaseException:
            if not run_task.done():
                run_task.cancel()
                await asyncio.gather(run_task, return_exceptions=True)
            raise

    async def _build_reason_provider(self):
        """Build the Coordinator Reason model separately from Worker engines."""

        def fallback(reason_code: str):
            # A selected Reason model must never fail silently into local
            # strategy logic.  Keep the fallback safe, but make the actual
            # provider choice visible in the same run's audit stream.
            if self._graph is not None:
                self._graph.emit_event(
                    actor="muteki-runtime",
                    event_type=EventType.REASON_MODEL_FALLBACK,
                    payload={"reason_code": str(reason_code)[:100]},
                )
            return self._reason_provider

        reason_id, _ = runtime_selection_from_hints(
            self.run.hints_json,
            fallback_engine_type=self.run.engine_type,
            fallback_model_config_id=self.run.model_config_id,
        )
        if not reason_id:
            return fallback("NO_REASON_MODEL_SELECTED")
        config = await self.session.get(ModelConfig, reason_id)
        if not config or not config.enabled or config.provider_type != "openai_compatible":
            return fallback("REASON_MODEL_UNSUPPORTED_OR_DISABLED")
        api_key = decrypt_api_key(config.encrypted_api_key)
        if not api_key:
            return fallback("REASON_MODEL_CREDENTIALS_UNAVAILABLE")
        # Coordinator Reason receives the full durable Blackboard summary and
        # may use a reasoning model.  The model-config timeout was originally
        # tuned for short Worker turns; reusing a 30s Worker window caused the
        # production Reason request to time out at exactly 30s and be mislabeled
        # as MODEL_UNAVAILABLE.  Keep the user setting as a lower bound source,
        # but give this separate Coordinator boundary a recovery-safe floor.
        reason_timeout_seconds = max(
            120.0,
            float(config.request_timeout_seconds or 30),
        )
        engine = OpenAICompatibleEngine(
            str(config.base_url or ""),
            api_key,
            str(config.model_name or ""),
            timeout=reason_timeout_seconds,
            action_protocol=str(config.action_protocol or "json_schema"),
            max_output_tokens=int(config.max_output_tokens or 2048),
            temperature=float(config.temperature or 0.0),
            max_retries=int(config.max_retries or 0),
            retry_base_seconds=float(config.retry_base_seconds or 1.0),
            rate_limit_cooldown_seconds=int(config.rate_limit_cooldown_seconds or 60),
        )
        self._reason_model = CoordinatorReasonModel(
            engine,
            model_config_id=str(config.id),
            model_name=str(config.model_name or config.name),
            fallback=self._reason_provider,
            run_id=self._run_id,
            challenge_id=str(self.challenge.id),
            mode=str(getattr(self.challenge, "mode", "ctf") or "ctf"),
            goal=str(getattr(self.challenge, "goal", "") or "") or None,
            usage_recorder=lambda trace: record_muteki_reason_usage(
                SessionLocal,
                run_id=self._run_id,
                model_config_id=str(config.id),
                model_name=str(config.model_name or config.name),
                trace=trace,
            ),
        )
        if self._graph is not None:
            self._graph.emit_event(
                actor="muteki-runtime",
                event_type=EventType.REASON_MODEL_SELECTED,
                payload={
                    "role": "coordinator_reason",
                    "model_config_id": str(config.id),
                    "model": str(config.model_name or config.name)[:120],
                },
            )
        return self._reason_model

    async def _openai_worker_action(
        self,
        job: WorkerJob,
        expected_tool: str,
        arguments: dict[str, Any],
    ) -> tuple[str, dict[str, Any]] | None:
        """Let an OpenAI-compatible Worker refine one declared Tool action.

        The Coordinator has already selected the Intent and tool domain.  The
        model may only return the same declared tool with bounded arguments;
        actual execution remains inside the existing ToolGateway adapter.
        """

        config_id = str((job.environment or {}).get("MUTEKI_MODEL_CONFIG_ID") or "")
        if not config_id:
            return None
        model = self._worker_models.get(config_id)
        if model is None:
            config = await self.session.get(ModelConfig, config_id)
            if not config or not config.enabled or config.provider_type != "openai_compatible":
                return None
            api_key = decrypt_api_key(config.encrypted_api_key)
            if not api_key:
                return None
            model = OpenAICompatibleEngine(
                str(config.base_url or ""),
                api_key,
                str(config.model_name or ""),
                timeout=float(config.request_timeout_seconds or 30),
                action_protocol=str(config.action_protocol or "json_schema"),
                max_output_tokens=int(config.max_output_tokens or 2048),
                temperature=float(config.temperature or 0.0),
                max_retries=int(config.max_retries or 0),
                retry_base_seconds=float(config.retry_base_seconds or 1.0),
                rate_limit_cooldown_seconds=int(config.rate_limit_cooldown_seconds or 60),
            )
            self._worker_models[config_id] = model
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a bounded Muteki Worker. The Coordinator already selected one tool. "
                    "Return exactly one ToolAction JSON. You may not change the tool name, add a new "
                    "tool, claim a finding, or return raw response data."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"goal": str(job.goal or "")[:1200], "tool_name": expected_tool, "arguments": _safe_worker_arguments(arguments)},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )[:5000],
            },
        ]
        try:
            action = await model.next_action(messages)
        except Exception:
            return None
        if getattr(action, "type", None) != "tool" or str(getattr(action, "tool_name", "")) != expected_tool:
            return None
        next_arguments = getattr(action, "arguments", None)
        if not isinstance(next_arguments, dict):
            return expected_tool, arguments
        refined = dict(next_arguments)
        for key, value in arguments.items():
            if _sensitive_argument_name(str(key)):
                refined[str(key)] = value
        return expected_tool, refined

    async def _worker_profiles(self, worker_backend: str) -> list[EngineProfile]:
        """Project the Run's independent Worker selections into Muteki profiles."""

        _, selections = runtime_selection_from_hints(
            self.run.hints_json,
            fallback_engine_type=self.run.engine_type,
            fallback_model_config_id=self.run.model_config_id,
        )
        profiles: list[EngineProfile] = []
        explicit_selections = bool(selections)
        for selection in selections:
            environment = {"MUTEKI_TARGET_URL": str(self.challenge.target_url or "")}
            driver_profile: dict[str, Any] = {}
            if selection.model_config_id:
                config = await self.session.get(ModelConfig, selection.model_config_id)
                if config is None or not config.enabled:
                    continue
                if selection.engine_type == "openai_compatible":
                    api_key = decrypt_api_key(config.encrypted_api_key)
                    if not api_key:
                        # Do not silently turn an explicitly selected Worker
                        # into Codex.  The official Coordinator must observe a
                        # failed/unsupported selection instead of changing the
                        # user's engine roster behind their back.
                        continue
                    capabilities = config.capabilities_json or {}
                    driver_profile = {
                        "id": selection.engine_id,
                        "name": selection.engine_id,
                        # OpenAI-compatible Workers use Muteki's native
                        # Chat Completions client.  They must not be routed
                        # through Codex EndpointDriver, whose default wire
                        # contract is /responses.
                        "engine": "openai_compatible",
                        "transport": "openai_compatible",
                        "protocol": "chat_completions",
                        "base_url": str(config.base_url or "").strip(),
                        "model": str(config.model_name or config.name).strip(),
                        "wire_api": str(capabilities.get("wire_api") or "chat_completions").strip(),
                        "api_key_ref": "env:OPENAI_API_KEY",
                    }
                    environment["OPENAI_API_KEY"] = api_key
                elif selection.engine_type == "codex_cli":
                    if config.provider_type != "codex_cli":
                        continue
                    driver_profile = {
                        "id": selection.engine_id,
                        "name": selection.engine_id,
                        "engine": "codex",
                        "transport": "codex_cli",
                        "protocol": "codex_cli",
                        "model": str(config.model_name or config.name).strip(),
                        "reasoning_effort": str(
                            (config.capabilities_json or {}).get("reasoning_effort") or "medium"
                        ).strip(),
                    }
                    base_url = str(config.base_url or "").strip()
                    if base_url:
                        wire_api = str((config.capabilities_json or {}).get("wire_api") or "responses").strip()
                        driver_profile["base_url"] = base_url
                        driver_profile["wire_api"] = wire_api
                        driver_profile["api_key_ref"] = "env:OPENAI_API_KEY"
                        api_key = decrypt_api_key(config.encrypted_api_key) if config.encrypted_api_key else ""
                        if api_key:
                            environment["OPENAI_API_KEY"] = api_key
                        else:
                            # Do not silently drop a selected API Worker.
                            continue
                    else:
                        # Local/subscription codex CLI: use account store.
                        codex_account_id = (
                            os.environ.get("MUTEKI_CODEX_ACCOUNT_ID")
                            or os.environ.get("MUTEKI_DEFAULT_ACCOUNT_ID")
                            or "codex-main"
                        ).strip()
                        environment["MUTEKI_CREDENTIAL_ACCOUNT_ID"] = codex_account_id
                        environment["MUTEKI_CODEX_ACCOUNT_ID"] = codex_account_id
                    environment["MUTEKI_WORKER_MODEL"] = driver_profile["model"]
                environment.update(
                    {
                        "MUTEKI_MODEL_CONFIG_ID": str(config.id),
                        "MUTEKI_MODEL_NAME": str(config.model_name or config.name)[:120],
                    }
                )
            # ``codex`` is the official vendored driver identity.  Other
            # identities remain explicit so Coordinator scheduling/audit never
            # silently collapses a selected engine into another provider.
            profiles.append(
                EngineProfile(
                    selection.engine_id,
                    environment=environment,
                    worker_class="reasoning" if selection.engine_type == "openai_compatible" else "code",
                    driver_profile=driver_profile,
                )
            )
        if profiles:
            return profiles
        if explicit_selections:
            raise ValueError("MUTEKI_WORKER_SELECTION_UNAVAILABLE")
        return [
            EngineProfile(
                "codex" if worker_backend in {"upstream_local", "upstream_container"} else "gateway-runner",
                environment={"MUTEKI_TARGET_URL": str(self.challenge.target_url or "")},
            )
        ]

    async def _record_official_worker_usage(self, job: WorkerJob, result: Any) -> None:
        """Project native Worker usage into the durable RunEvent stream."""

        await record_official_worker_usage(
            SessionLocal,
            run_id=self._run_id,
            worker_id=str(getattr(job, "worker_id", "") or ""),
            intent_id=str(getattr(job, "intent_id", "") or "") or None,
            result=result,
        )

    def _reason_provider(self, snapshot: dict) -> list[dict[str, Any]]:
        metadata = self.challenge.metadata_json or {}
        classification = classify_challenge(metadata, snapshot.get("facts", ()))
        if self._public_credentials is None:
            self._public_credentials = self._recover_public_credentials()
        if self._graph is not None:
            self._graph.emit_event(
                actor="muteki-runtime",
                event_type="runtime.reason.context",
                payload={
                    "classification": classification.classification if classification else None,
                    "public_demo_account_available": bool(self._public_credentials),
                },
            )
        strategy = MutekiStrategyPlanner(
            target_url=str(self.challenge.target_url or ""),
            metadata=metadata,
            public_credentials=self._public_credentials,
        )
        # The reusable Blackboard strategy owns the normal OODA continuation
        # now: authentication bootstrap, classified web lanes, and the
        # bounded IDOR/path routes all start here. A small generic fallback
        # below remains for incomplete historical fact formats only.
        planned = strategy.plan(snapshot)
        if planned or (classification and classification.classification == "SQLI"):
            return planned
        # Keep this compatibility path only for low-confidence or historical
        # snapshots that the reusable strategy cannot parse. High-confidence
        # classifications must remain wholly owned by MutekiStrategyPlanner.
        if (
            self._public_credentials
            and (classification is None or classification.confidence < 70)
        ):
            facts = "\n".join(
                str(item.get("content") or "")
                for item in snapshot.get("facts", ())
                if isinstance(item, dict)
            )
            folded = facts.casefold()
            target = str(self.challenge.target_url or "").rstrip("/")
            session_name = "muteki-recon"
            if "request_method=post" not in folded or f"request_url={target}/login" not in folded:
                username, password = self._public_credentials
                return [{
                    "goal": "authenticate using credentials explicitly disclosed by the target",
                    "worker_class": "exploit",
                    "rationale": "Race observed a public demo account; establish one bounded session before probing protected business routes.",
                    "payload": {"tool_name": "http_session_request", "arguments": {
                        "session_name": session_name,
                        "method": "POST",
                        "url": f"{target}/login",
                        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
                        "body": f"username={username}&password={password}",
                        "follow_redirects": False,
                    }},
                }]
            for endpoint in _protected_session_endpoints(snapshot, target):
                if f"request_url={endpoint}" in folded:
                    continue
                return [{
                    "goal": f"inspect authenticated endpoint {endpoint}",
                    "worker_class": "exploit",
                    "rationale": "Reuse the established session to inspect the next evidence-backed protected endpoint.",
                    "payload": {"tool_name": "http_session_request", "arguments": {
                        "session_name": session_name,
                        "method": "GET",
                        "url": endpoint,
                        "follow_redirects": False,
                    }},
                }]
        # All remaining classifications use the same graph-driven playbook.
        # A low-confidence GENERIC_WEB result is deliberately treated as
        # reconnaissance only; it must never fall through to SQL actions.
        # A high-confidence non-SQL classification must never fall through to
        # MutekiReason's empty-argument fallback.  That fallback is useful for
        # unit-level graph reasoning, but it cannot satisfy the production
        # Gateway schema.  Use one observed same-origin endpoint as a valid,
        # bounded read-only continuation instead.
        if classification is None or classification.confidence < 70:
            endpoint = _next_observed_endpoint(snapshot, str(self.challenge.target_url or ""))
            if endpoint:
                classification_name = classification.classification if classification else "GENERIC_WEB"
                tool_name = "http_session_request" if classification_name in {"IDOR", "JWT"} else "http_request"
                arguments: dict[str, Any] = {"method": "GET", "url": endpoint, "follow_redirects": False}
                if tool_name == "http_session_request":
                    arguments["session_name"] = "muteki-recon"
                return [{
                    "goal": f"inspect classified {classification_name.lower()} endpoint {endpoint}",
                    "worker_class": "recon",
                    "rationale": "Continue with one evidence-backed endpoint using a complete Tool Gateway request contract.",
                    "payload": {"tool_name": tool_name, "arguments": arguments, "classification": classification_name},
                }]
        if snapshot.get("facts"):
            return []
        target = str(self.challenge.target_url or "")
        if not target:
            return []
        return [{
            "goal": "establish target HTTP baseline",
            "worker_class": "gateway",
            "rationale": "Start with one bounded request through the existing Tool Gateway.",
            "payload": {"tool_name": "http_request", "arguments": {"method": "GET", "url": target}},
        }]

    def _recover_public_credentials(self) -> tuple[str, str] | None:
        """Recover only explicitly published demo credentials from this Run.

        The Gateway may keep the full bounded response in an Evidence
        artifact while returning only its model view to the Worker.  This
        fallback reads only this Run's ``outputs`` directory and accepts the
        same explicit public presentation pattern as Race.  It never scans
        source files, challenge metadata, or other workspaces, and the values
        are retained only in runtime memory for one login Intent.
        """

        outputs = Path(self._workspace_path).resolve() / "outputs"
        if not outputs.is_dir():
            return None
        for path in sorted(outputs.glob("*.txt"))[:40]:
            try:
                content = path.read_text(encoding="utf-8", errors="replace")[:1_000_000]
            except OSError:
                continue
            credentials = _public_demo_credentials(content)
            if credentials:
                return credentials
        return None

    async def _disabled_compat_worker(self, job: WorkerJob) -> WorkerOutcome:
        """Fail closed if a production job ever bypasses the container route."""

        if self._graph is not None:
            self._graph.add_dead_end(
                actor=job.worker_id,
                description="LEGACY_WORKER_ROUTE_DISABLED_CONTAINER_ONLY",
            )
        return WorkerOutcome(job.worker_id, "FAILED", result="MUTEKI_CONTAINER_ONLY")

    def _distill(self, graph_path: str, reason: str = "") -> None:
        """Distill a solved run into a reusable template (\\u00a716)."""
        import traceback
        from pathlib import Path

        try:
            from muteki.learning.distill import (
                TemplateStore,
                distill_from_events,
                distill_and_store,
            )
            from muteki.swarm.shared_graph import SharedGraph

            g = SharedGraph(Path(graph_path), challenge_id=self.challenge.id)
            store = TemplateStore(
                root=Path(self._workspace_path).resolve().parent.parent
                / "knowledge"
            )
            tpl = distill_from_events(g, winner=self._run_id)
            store.save(tpl)
            self._graph.emit_event(
                actor="muteki-runtime",
                event_type=EventType.RUN_FINISHED,
                payload={
                    "distilled": True,
                    "template_name": tpl.name,
                    "template_category": tpl.category,
                    "template_keywords": tpl.keywords[:8],
                },
            )
        except Exception:
            self._graph.add_dead_end(
                actor="muteki-runtime",
                description=f"DISTILL_FAILED: {traceback.format_exc()[:200]}",
            )


def _sensitive_argument_name(name: str) -> bool:
    folded = name.casefold()
    return any(token in folded for token in ("password", "passwd", "secret", "token", "cookie", "authorization", "api_key"))


def _safe_worker_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): "<redacted>" if _sensitive_argument_name(str(key)) else value
        for key, value in arguments.items()
    }


__all__ = ["MutekiRuntime"]


def _protected_session_endpoints(snapshot: dict[str, Any], target: str) -> list[str]:
    """Return bounded, same-origin routes that Race marked as protected."""

    from urllib.parse import urlparse

    origin = urlparse(target).netloc.casefold()
    candidates: list[str] = []
    for item in snapshot.get("facts", ()):
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, str):
            continue
        try:
            value = json.loads(content)
        except (TypeError, ValueError):
            continue
        if value.get("type") == "ENDPOINT_OBSERVED":
            entries = [value]
        elif value.get("type") == "ENDPOINTS_DISCOVERED":
            entries = [entry for entry in value.get("endpoints", ()) if isinstance(entry, dict)]
        else:
            entries = []
        for entry in entries:
            endpoint = str(entry.get("endpoint") or "")
            if not endpoint or urlparse(endpoint).netloc.casefold() != origin:
                continue
            path = urlparse(endpoint).path.rstrip("/").casefold()
            if path in {"", "/login"}:
                continue
            status = int(entry.get("status_code") or 0)
            if bool(entry.get("auth_required")) or status in {301, 302, 303, 307, 308, 401, 403}:
                if endpoint not in candidates:
                    candidates.append(endpoint)
    return candidates[:8]


def _next_observed_endpoint(snapshot: dict[str, Any], target: str) -> str | None:
    """Return one unrequested, same-origin endpoint from Race facts."""

    from urllib.parse import urlparse

    origin = urlparse(target).netloc.casefold()
    requested = {
        value.casefold()
        for item in snapshot.get("facts", ())
        if isinstance(item, dict)
        for value in re.findall(r"request_url=([^;\s]+)", str(item.get("content") or ""), re.IGNORECASE)
    }
    candidates: list[str] = []
    for item in snapshot.get("facts", ()):
        if not isinstance(item, dict):
            continue
        try:
            value = json.loads(str(item.get("content") or ""))
        except (TypeError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        entries = [value] if value.get("type") == "ENDPOINT_OBSERVED" else [entry for entry in value.get("endpoints", ()) if isinstance(entry, dict)] if value.get("type") == "ENDPOINTS_DISCOVERED" else []
        for entry in entries:
            endpoint = str(entry.get("endpoint") or "")
            if not endpoint or urlparse(endpoint).netloc.casefold() != origin:
                continue
            if endpoint.casefold() in requested or endpoint not in candidates:
                if endpoint.casefold() not in requested and endpoint not in candidates:
                    candidates.append(endpoint)
    return candidates[0] if candidates else None
