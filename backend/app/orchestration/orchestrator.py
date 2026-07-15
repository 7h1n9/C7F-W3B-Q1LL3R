import asyncio
import contextlib
import hashlib
import json
from pathlib import Path
from time import monotonic

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.exceptions import DomainError
from app.engines import (
    BridgeRateLimitError,
    BridgeUnavailableError,
    CodexSdkEngine,
    MockSolveEngine,
    ModelProviderError,
    ModelRateLimitError,
    ModelUnavailableError,
    OpenAICompatibleEngine,
    SolveEngine,
)
from app.models.challenge import Challenge
from app.models.model_config import ModelConfig
from app.models.run import (
    AgentTurn,
    Artifact,
    FlagCandidate,
    Observation,
    RunUserInput,
    SolveRun,
    ToolCall,
)
from app.models.skill import RunSkillSnapshot, Skill
from app.orchestration.state_machine import TERMINAL, RunStatus, transition
from app.schemas.agent import ActionHypothesis, FinishAction, SkillAction, ToolAction
from app.services.action_fingerprint import fingerprint_action
from app.services.codex_materializer import codex_materializer
from app.services.context_builder import context_builder
from app.services.crypto import decrypt_api_key
from app.services.events import event_service
from app.services.finish_gate import finish_gate
from app.services.flags import flag_service
from app.services.hypotheses import hypothesis_service
from app.services.progress_evaluator import progress_evaluator
from app.services.reports import report_service
from app.services.run_attempts import run_attempt_service
from app.services.runner_client import runner_client
from app.services.solver_state import solver_state_service
from app.services.tool_permissions import effective_tools_for
from app.tools.gateway import tool_gateway
from app.tools.registry import load_tool_definitions

CTF_PHASE_ORDER = (
    "INTAKE",
    "BASELINE",
    "MAPPING",
    "HYPOTHESIS",
    "TESTING",
    "CHAINING",
    "FLAG_SEARCH",
    "FLAG_VERIFICATION",
    "REPORTING",
)
CTF_PHASE_INDEX = {phase: index for index, phase in enumerate(CTF_PHASE_ORDER)}


class SolveOrchestrator:
    def __init__(self, engine_factory=None) -> None:
        self.engine_factory = engine_factory
        self.active_engines: dict[str, object] = {}
        self.active_tasks: dict[str, asyncio.Task[None]] = {}

    async def _correct_phase(self, session, run: SolveRun, requested: str | None) -> None:
        phase = str(requested or "").upper()
        current = str(run.current_phase or "").upper()
        if phase not in CTF_PHASE_INDEX or phase == current:
            return
        # Do not let a generic model label (for example INTAKE) move a run
        # backwards after useful evidence has already advanced the method.
        if current in CTF_PHASE_INDEX and CTF_PHASE_INDEX[phase] < CTF_PHASE_INDEX[current]:
            return
        previous = run.current_phase
        run.current_phase = phase
        await session.commit()
        await solver_state_service.sync_from_run(session, run)
        await event_service.append(session, run.id, "phase.corrected", {"previous_phase": previous, "phase": phase, "source": "model_action"})

    async def _skill_decision_required(self, session, run: SolveRun) -> bool:
        state = await solver_state_service.load(session, run.id)
        if not state:
            return False
        active = set(state.active_skill_ids_json or [])
        specialists = list((await session.scalars(select(Skill).where(Skill.id.in_(active), Skill.skill_kind == "SPECIALIST"))).all()) if active else []
        return not specialists and any(item.get("confidence", 0) >= 80 and item.get("supporting_fact_ids") for item in (state.skill_recommendations_json or []))

    async def build_engine(self, run: SolveRun, session, attempt=None, lease=None) -> object:
        if self.engine_factory:
            return self.engine_factory(run)
        if run.engine_type == "codex_sdk":
            challenge = await session.get(Challenge, run.challenge_id)
            return CodexSdkEngine(
                get_settings().codex_bridge_url,
                run.workspace_path,
                thread_id=run.codex_thread_id,
                scope={
                    "run_id": run.id,
                    "challenge_id": run.challenge_id,
                    "workspace_root": run.workspace_path,
                    "allowed_hosts": list(challenge.allowed_hosts or []) if challenge else [],
                    "attempt_id": attempt.id if attempt else None,
                    "lease_token": lease.lease_token if lease else None,
                },
            )
        if run.engine_type == "openai_compatible":
            config = (
                await session.get(ModelConfig, run.model_config_id) if run.model_config_id else None
            )
            if not config or not config.enabled or not config.base_url or not config.model_name:
                raise ValueError("OpenAI-compatible engine requires an enabled model configuration")
            return OpenAICompatibleEngine(
                config.base_url,
                decrypt_api_key(config.encrypted_api_key),
                config.model_name,
                timeout=config.request_timeout_seconds,
                action_protocol=config.action_protocol,
                max_output_tokens=config.max_output_tokens,
                temperature=config.temperature,
                max_retries=config.max_retries,
                retry_base_seconds=config.retry_base_seconds,
                rate_limit_cooldown_seconds=config.rate_limit_cooldown_seconds,
            )
        return MockSolveEngine()

    async def _transition(self, session, run: SolveRun, target: RunStatus) -> None:
        if RunStatus(run.status) == target:
            return
        transition(run, target)
        await session.commit()
        await solver_state_service.sync_from_run(session, run)
        await event_service.append(session, run.id, "run.status_changed", {"status": run.status})

    async def _consume_queued_inputs(self, session, run: SolveRun, attempt=None) -> str | None:
        queued = list((await session.scalars(select(RunUserInput).where(RunUserInput.run_id == run.id, RunUserInput.status == "QUEUED").order_by(RunUserInput.revision))).all())
        if not queued:
            return None
        from datetime import UTC, datetime
        now = datetime.now(UTC)
        for item in queued:
            item.status, item.consumed_at, item.consumed_by_attempt_id = "CONSUMED", now, (attempt.id if attempt else None)
        await session.commit()
        await event_service.append(session, run.id, "user.input_consumed", {"revisions": [item.revision for item in queued], "attempt_id": attempt.id if attempt else None})
        await event_service.append(session, run.id, "run.guidance_updated", {"context_revision": run.context_revision})
        return "\n\n".join(f"用户补充信息 v{item.revision}: {item.content}" for item in queued)

    async def _stop_if_no_progress(
        self,
        session,
        run: SolveRun,
        challenge: Challenge,
        no_progress_count: int,
    ) -> bool:
        if no_progress_count < 6:
            return False
        await self._transition(session, run, RunStatus.REPORTING)
        await report_service.generate(
            session,
            run,
            challenge,
            "unsolved",
            "连续 6 次动作没有产生新的结构化证据，任务受控结束。",
        )
        await self._transition(session, run, RunStatus.COMPLETED_UNSOLVED)
        return True

    async def _resolve_skill(self, session, skill_id: str | None, skill_name: str | None) -> Skill | None:
        if skill_id:
            skill = await session.get(Skill, skill_id)
            if skill:
                return skill
        if skill_name:
            return await session.scalar(select(Skill).where(Skill.name == skill_name))
        return None

    async def _ensure_skill_snapshot(
        self, session, run: SolveRun, skill: Skill, priority: int = 1000
    ) -> bool:
        snapshot = await session.scalar(
            select(RunSkillSnapshot).where(
                RunSkillSnapshot.run_id == run.id, RunSkillSnapshot.skill_id == skill.id
            )
        )
        if snapshot:
            return False
        session.add(
            RunSkillSnapshot(
                run_id=run.id,
                skill_id=skill.id,
                skill_name=skill.name,
                skill_version=skill.version,
                content_snapshot=skill.content_markdown,
                allowed_tools_snapshot=skill.allowed_tools,
                config_snapshot={},
                priority=priority,
            )
        )
        await session.commit()
        return True

    async def _handle_skill_action(
        self,
        session,
        run: SolveRun,
        challenge: Challenge,
        action: SkillAction,
    ) -> bool:
        if action.operation == "decline" and not (action.skill_id or action.skill_name):
            await event_service.append(session, run.id, "skill.declined", {"reason": action.reason, "phase": action.phase})
            await solver_state_service.record_rejected_path(session, run.id, {"source": "skill_decision", "reason": action.reason})
            await self._transition(session, run, RunStatus.PLANNING)
            return True
        skill = await self._resolve_skill(session, action.skill_id, action.skill_name)
        if not skill:
            await event_service.append(
                session,
                run.id,
                "skill.activation_rejected",
                {
                    "skill_id": action.skill_id,
                    "skill_name": action.skill_name,
                    "operation": action.operation,
                    "error_code": "SKILL_NOT_FOUND",
                    "reason": "Skill not found.",
                },
            )
            await solver_state_service.record_rejected_path(
                session,
                run.id,
                {
                    "source": "skill_action",
                    "operation": action.operation,
                    "error_code": "SKILL_NOT_FOUND",
                    "skill_id": action.skill_id,
                    "skill_name": action.skill_name,
                },
            )
            return False
        active_state = await solver_state_service.load(session, run.id)
        active_ids = set(active_state.active_skill_ids_json or []) if active_state else set()
        active_skill_rows = (
            list((await session.scalars(select(Skill).where(Skill.id.in_(active_ids)))).all())
            if active_ids
            else []
        )
        active_skill_names = {item.name for item in active_skill_rows} | {
            item.display_name for item in active_skill_rows
        } | active_ids
        if action.operation == "deactivate":
            if skill.skill_kind in {"CORE", "METHODOLOGY"}:
                await event_service.append(
                    session,
                    run.id,
                    "skill.activation_rejected",
                    {
                        "skill_id": skill.id,
                        "skill_name": skill.name,
                        "operation": action.operation,
                        "error_code": "SKILL_NOT_DEACTIVATABLE",
                        "reason": "CORE and methodology skills cannot be deactivated.",
                    },
                )
                return False
            changed = await solver_state_service.deactivate_skill(session, run.id, skill.id)
            if changed:
                await event_service.append(
                    session,
                    run.id,
                    "skill.deactivated",
                    {
                        "skill_id": skill.id,
                        "skill_name": skill.name,
                        "source": "action",
                        "phase": action.phase,
                    },
                )
                await solver_state_service.record_progress(session, run.id, True)
                await self._transition(session, run, RunStatus.PLANNING)
                return True
            await event_service.append(
                session,
                run.id,
                "skill.activation_rejected",
                {
                    "skill_id": skill.id,
                    "skill_name": skill.name,
                    "operation": action.operation,
                    "error_code": "SKILL_ALREADY_INACTIVE",
                    "reason": "Skill is not active.",
                },
            )
            return False
        if action.operation == "decline":
            await event_service.append(session, run.id, "skill.declined", {"skill_id": skill.id, "skill_name": skill.name, "reason": action.reason, "phase": action.phase})
            await solver_state_service.record_rejected_path(session, run.id, {"source": "skill_decision", "skill_id": skill.id, "reason": action.reason})
            await self._transition(session, run, RunStatus.PLANNING)
            return True
        if skill.skill_kind == "SPECIALIST" and challenge.challenge_type not in (skill.challenge_types or []):
            await event_service.append(
                session,
                run.id,
                "skill.activation_rejected",
                {
                    "skill_id": skill.id,
                    "skill_name": skill.name,
                    "operation": action.operation,
                    "error_code": "SKILL_NOT_APPLICABLE",
                    "reason": "Skill is not applicable to this challenge type.",
                },
            )
            return False
        if skill.skill_kind == "SPECIALIST" and action.operation == "activate":
            state = await solver_state_service.load(session, run.id)
            if not action.supporting_evidence and not (state and state.confirmed_facts_json):
                await event_service.append(session, run.id, "skill.activation_rejected", {"skill_id": skill.id, "skill_name": skill.name, "error_code": "SKILL_EVIDENCE_REQUIRED", "reason": "Specialist skills require structured evidence before activation."})
                await solver_state_service.record_rejected_path(session, run.id, {"source": "skill", "code": "SKILL_EVIDENCE_REQUIRED", "skill_id": skill.id})
                return False
        if skill.ctf_phases:
            current_phase = str(run.current_phase or "").upper()
            allowed_phases = [str(item).upper() for item in skill.ctf_phases]
            if current_phase not in allowed_phases:
                current_index = CTF_PHASE_INDEX.get(current_phase, -1)
                next_phases = sorted(
                    (
                        phase
                        for phase in allowed_phases
                        if CTF_PHASE_INDEX.get(phase, -1) >= current_index
                    ),
                    key=lambda phase: CTF_PHASE_INDEX.get(phase, 10_000),
                )
                if next_phases:
                    target_phase = next_phases[0]
                    await self._correct_phase(session, run, target_phase)
                    await event_service.append(
                        session,
                        run.id,
                        "skill.phase_advanced",
                        {
                            "skill_id": skill.id,
                            "skill_name": skill.name,
                            "from_phase": current_phase,
                            "to_phase": target_phase,
                            "reason": "Specialist skill activation requires this phase.",
                        },
                    )
                else:
                    await event_service.append(
                        session,
                        run.id,
                        "skill.activation_rejected",
                        {
                            "skill_id": skill.id,
                            "skill_name": skill.name,
                            "operation": action.operation,
                            "error_code": "SKILL_PHASE_NOT_APPLICABLE",
                            "reason": "Skill is not applicable to the current phase.",
                        },
                    )
                    return False
        prerequisites = [str(item).lower() for item in (skill.prerequisites or [])]
        if prerequisites and not all(
            any(required in candidate for candidate in active_skill_names) for required in prerequisites
        ):
            await event_service.append(
                session,
                run.id,
                "skill.activation_rejected",
                {
                    "skill_id": skill.id,
                    "skill_name": skill.name,
                    "operation": action.operation,
                    "error_code": "SKILL_PREREQUISITE_NOT_MET",
                    "reason": "Required prerequisite skills are not active.",
                },
            )
            return False
        permitted_tools = await effective_tools_for(session, run, challenge)
        if skill.required_tools and not set(skill.required_tools).issubset(permitted_tools):
            await event_service.append(
                session,
                run.id,
                "skill.activation_rejected",
                {
                    "skill_id": skill.id,
                    "skill_name": skill.name,
                    "operation": action.operation,
                    "error_code": "SKILL_REQUIRED_TOOL_UNAVAILABLE",
                    "reason": "Required tools are not available for this run.",
                },
            )
            return False
        if action.operation == "inspect":
            await event_service.append(
                session,
                run.id,
                "skill.requested",
                {
                    "skill_id": skill.id,
                    "skill_name": skill.name,
                    "operation": action.operation,
                    "phase": action.phase,
                    "reason": action.reason,
                    "supporting_evidence": action.supporting_evidence,
                },
            )
            await solver_state_service.record_progress(session, run.id, True)
            await self._transition(session, run, RunStatus.PLANNING)
            return True
        if skill.id in active_ids:
            await event_service.append(
                session,
                run.id,
                "skill.activation_rejected",
                {
                    "skill_id": skill.id,
                    "skill_name": skill.name,
                    "operation": action.operation,
                    "error_code": "SKILL_ALREADY_ACTIVE",
                    "reason": "Skill is already active.",
                },
            )
            return False
        snapshot_created = await self._ensure_skill_snapshot(session, run, skill)
        activated = await solver_state_service.activate_skill(session, run.id, skill.id)
        if activated:
            await event_service.append(
                session,
                run.id,
                "skill.requested",
                {
                    "skill_id": skill.id,
                    "skill_name": skill.name,
                    "operation": action.operation,
                    "phase": action.phase,
                    "reason": action.reason,
                    "supporting_evidence": action.supporting_evidence,
                    "expected_use": action.expected_use,
                },
            )
            if snapshot_created:
                await event_service.append(
                    session,
                    run.id,
                    "skill.snapshot_created",
                    {"skill_id": skill.id, "skill_name": skill.name, "source": "action"},
                )
            await event_service.append(
                session,
                run.id,
                "skill.activated",
                {"skill_id": skill.id, "skill_name": skill.name, "source": "action"},
            )
            await solver_state_service.record_progress(session, run.id, True)
            await self._transition(session, run, RunStatus.PLANNING)
            return True
        await event_service.append(
            session,
            run.id,
            "skill.activation_rejected",
            {
                "skill_id": skill.id,
                "skill_name": skill.name,
                "operation": action.operation,
                "error_code": "SKILL_DISABLED_FOR_RUN",
                "reason": "Skill could not be activated for this run.",
            },
        )
        return False

    async def start(self, run_id: str, user_message: str | None = None) -> None:
        task = asyncio.current_task()
        if task:
            self.active_tasks[run_id] = task
        async with SessionLocal() as session:
            attempt = None
            lease = None
            try:
                run = await session.scalar(select(SolveRun).where(SolveRun.id == run_id))
                if not run:
                    return
                try:
                    attempt, lease = await run_attempt_service.begin(session, run)
                except DomainError:
                    raise
                except Exception as error:
                    # A newly created attempt is the first database write in
                    # the background task.  If that write fails, the old
                    # implementation left the run in CREATED and then tried
                    # to use the failed SQLAlchemy transaction again, hiding
                    # the real cause behind PendingRollbackError.  Roll back,
                    # persist a clear terminal state in a clean transaction,
                    # and stop the finalizer from touching the failed attempt.
                    await session.rollback()
                    attempt = None
                    failed_run = await session.scalar(select(SolveRun).where(SolveRun.id == run_id))
                    if failed_run and RunStatus(failed_run.status) not in TERMINAL:
                        failed_run.last_error_code = "DATABASE_ERROR"
                        failed_run.last_error_message = str(error)[:4000]
                        await self._transition(session, failed_run, RunStatus.FAILED_ENGINE)
                        await session.commit()
                        await event_service.append(
                            session,
                            run_id,
                            "run.failed",
                            {"code": "DATABASE_ERROR", "message": str(error)[:1000]},
                        )
                    return
                if run.status == RunStatus.CREATED and run.engine_type != "mock":
                    try:
                        await runner_client.sync_workspace(run.id, Path(run.workspace_path))
                    except Exception as error:
                        run.last_error_code = "RUNNER_UNAVAILABLE"
                        run.last_error_message = str(error)[:4000]
                        await self._transition(session, run, RunStatus.FAILED_RUNNER)
                        await event_service.append(
                            session,
                            run.id,
                            "run.failed",
                            {"code": "RUNNER_UNAVAILABLE", "message": str(error)[:1000]},
                        )
                        return
                engine = await self.build_engine(run, session, attempt, lease)
                self.active_engines[run_id] = engine
                if run.engine_type == "openai_compatible":
                    await self._run_openai(session, run, engine, user_message, attempt, lease)
                else:
                    await self._run_event_engine(session, run, engine, user_message, attempt, lease)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if "run" in locals() and RunStatus(run.status) not in TERMINAL:
                    if isinstance(error, ModelRateLimitError):
                        code = "MODEL_RATE_LIMITED"
                    elif isinstance(error, BridgeRateLimitError):
                        code = "CODEX_BRIDGE_RATE_LIMITED"
                    elif isinstance(error, ModelUnavailableError):
                        code = "MODEL_UNAVAILABLE"
                    elif isinstance(error, ModelProviderError):
                        code = error.code
                    elif isinstance(error, BridgeUnavailableError):
                        code = "CODEX_BRIDGE_UNAVAILABLE"
                    elif isinstance(error, DomainError):
                        code = error.code
                    else:
                        code = "ENGINE_ERROR"
                    run.last_error_code, run.last_error_message = code, str(error)[:4000]
                    await self._transition(
                        session,
                        run,
                        RunStatus.PAUSED_RATE_LIMIT
                        if isinstance(error, ModelRateLimitError)
                        else RunStatus.FAILED_ENGINE,
                    )
                    await event_service.append(
                        session,
                        run_id,
                        "run.failed",
                        {"code": code, "message": str(error)[:1000]},
                    )
            finally:
                if attempt is not None and "run" in locals():
                    await run_attempt_service.finish(session, run, attempt, lease)
                self.active_engines.pop(run_id, None)
                self.active_tasks.pop(run_id, None)
                close = getattr(locals().get("engine"), "close", None)
                if close is not None:
                    await close()

    async def _run_openai(
        self,
        session,
        run: SolveRun,
        engine: OpenAICompatibleEngine,
        user_message: str | None,
        attempt=None,
        lease=None,
    ) -> None:
        if run.status == RunStatus.CREATED:
            await self._transition(session, run, RunStatus.PREPARING)
            await event_service.append(session, run.id, "run.started", {})
            await self._transition(session, run, RunStatus.ANALYZING)
        elif run.status in {
            RunStatus.WAITING_USER,
            RunStatus.PAUSED_RATE_LIMIT,
            RunStatus.PAUSED_CHECKPOINT,
            RunStatus.PAUSED_RECOVERY,
            RunStatus.WAITING_CONFIGURATION,
        }:
            await self._transition(session, run, RunStatus.PLANNING)
        else:
            raise DomainError("RUN_INVALID_STATE", "Run cannot be started from its current state.")
        challenge = await session.get(Challenge, run.challenge_id)
        if not challenge:
            raise ValueError("challenge not found")
        started = monotonic()
        consecutive_runner_failures = 0
        last_runner_failure: tuple[str, str] | None = None
        while run.agent_step_count < run.max_agent_steps:
            await run_attempt_service.heartbeat(session, attempt, lease)
            if RunStatus(run.status) == RunStatus.CANCELLED:
                return
            if monotonic() - started > run.max_runtime_seconds:
                await self._transition(session, run, RunStatus.TIMEOUT)
                return
            if RunStatus(run.status) in {RunStatus.ANALYZING, RunStatus.EVALUATING}:
                await self._transition(session, run, RunStatus.PLANNING)
            messages = await context_builder.build(session, run, challenge)
            queued_input = await self._consume_queued_inputs(session, run, attempt)
            decision_required = await self._skill_decision_required(session, run)
            if decision_required:
                messages.append({"role": "system", "content": "SKILL_DECISION_REQUIRED: before any tool action, return SkillAction with operation activate, inspect, or decline and provide a reason."})
            if queued_input:
                messages.append({"role": "user", "content": queued_input})
            if user_message:
                messages.append({"role": "user", "content": f"User supplied: {user_message}"})
                user_message = None
            action_started = monotonic()
            try:
                action = await engine.next_action(messages)
            except Exception:
                # Preserve provider parse/retry telemetry even when no action
                # can be executed. This makes FAILED_ENGINE diagnosable without
                # persisting secrets or the full prompt.
                trace = getattr(engine, "last_trace", {})
                session.add(
                    AgentTurn(
                        run_id=run.id,
                        step_number=run.agent_step_count + 1,
                        model_config_id=run.model_config_id,
                        action_protocol=getattr(engine, "action_protocol", "json_schema"),
                        prompt_hash=hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                        context_size_chars=sum(len(str(item.get("content", ""))) for item in messages),
                        provider_request_id=trace.get("provider_request_id"),
                        latency_ms=trace.get("latency_ms") or round((monotonic() - action_started) * 1000),
                        input_tokens=trace.get("input_tokens"),
                        output_tokens=trace.get("output_tokens"),
                        parse_attempts=trace.get("parse_attempts", 0),
                        parse_error_code=trace.get("parse_error_code") or "ENGINE_ACTION_FAILED",
                        response_excerpt_redacted=trace.get("response_excerpt"),
                        action_json=trace.get("action") or {},
                    )
                )
                await session.commit()
                raise
            trace = getattr(engine, "last_trace", {})
            session.add(
                AgentTurn(
                    run_id=run.id,
                    step_number=run.agent_step_count + 1,
                    model_config_id=run.model_config_id,
                    action_protocol=getattr(engine, "action_protocol", "json_schema"),
                    prompt_hash=hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                    context_size_chars=sum(len(str(item.get("content", ""))) for item in messages),
                    provider_request_id=trace.get("provider_request_id"),
                    latency_ms=trace.get("latency_ms") or round((monotonic() - action_started) * 1000),
                    input_tokens=trace.get("input_tokens"),
                    output_tokens=trace.get("output_tokens"),
                    parse_attempts=trace.get("parse_attempts", 1),
                    parse_error_code=trace.get("parse_error_code"),
                    response_excerpt_redacted=trace.get("response_excerpt"),
                    action_json=trace.get("action") or action.model_dump(),
                )
            )
            run.agent_step_count += 1
            await session.commit()
            interval = max(1, int(run.agent_checkpoint_interval or 30))
            if run.agent_step_count % interval == 0:
                await self._transition(session, run, RunStatus.PAUSED_CHECKPOINT)
                await event_service.append(
                    session,
                    run.id,
                    "run.checkpoint_reached",
                    {
                        "step": run.agent_step_count,
                        "interval": interval,
                        "phase": run.current_phase,
                        "remaining_steps": max(0, run.max_agent_steps - run.agent_step_count),
                    },
                )
                return
            await self._correct_phase(session, run, getattr(action, "phase", None))
            hypothesis_payload = getattr(action, "hypothesis", None)
            if isinstance(hypothesis_payload, ActionHypothesis):
                hypothesis_payload = hypothesis_payload.model_dump()
            await event_service.append(
                session,
                run.id,
                "agent.action_requested",
                {
                    "type": action.type,
                    "phase": getattr(action, "phase", None),
                    "objective": getattr(action, "objective", None),
                    "hypothesis": hypothesis_payload,
                    "reason": action.reason
                    if isinstance(action, (ToolAction, SkillAction))
                    else action.summary,
                    "expected_evidence": getattr(action, "expected_evidence", None),
                    "success_condition": getattr(action, "success_condition", None),
                    "failure_pivot": getattr(action, "failure_pivot", None),
                    "retry_reason": getattr(action, "retry_reason", None),
                    "activate_skill": getattr(action, "activate_skill", None),
                    "operation": getattr(action, "operation", None),
                    "skill_id": getattr(action, "skill_id", None),
                    "skill_name": getattr(action, "skill_name", None),
                    "supporting_evidence": getattr(action, "supporting_evidence", None),
                    "expected_use": getattr(action, "expected_use", None),
                },
            )
            decision_card = getattr(action, "decision_card", None)
            if decision_card is not None:
                decision_card = decision_card.model_dump()
            else:
                decision_card = {
                    "known_facts": "结构化 SolverState 与最新 ToolModelView",
                    "core_question": str(getattr(action, "objective", "Continue the authorized investigation")),
                    "discriminates": [str(getattr(action, "success_condition", "")), str(getattr(action, "failure_pivot", ""))],
                    "success_signal": str(getattr(action, "expected_evidence", "")),
                    "failure_pivot": str(getattr(action, "failure_pivot", "")),
                }
            await solver_state_service.record_decision_card(session, run.id, decision_card)
            hypothesis_item = None
            if isinstance(action, SkillAction):
                handled = await self._handle_skill_action(session, run, challenge, action)
                if not handled:
                    no_progress_count = await solver_state_service.record_progress(
                        session, run.id, False
                    )
                    await event_service.append(
                        session,
                        run.id,
                        "agent.no_progress",
                        {
                            "skill_id": action.skill_id,
                            "skill_name": action.skill_name,
                            "operation": action.operation,
                            "no_progress_count": no_progress_count,
                        },
                    )
                    if no_progress_count >= 2:
                        await event_service.append(
                            session,
                            run.id,
                            "agent.replan_required",
                            {"reason": "Repeated no-progress actions"},
                        )
                    if await self._stop_if_no_progress(
                        session, run, challenge, no_progress_count
                    ):
                        return
                continue
            if isinstance(action, ToolAction):
                if decision_required:
                    await event_service.append(session, run.id, "agent.action_rejected", {"type": "tool", "code": "SKILL_DECISION_REQUIRED"})
                    await solver_state_service.record_rejected_path(session, run.id, {"code": "SKILL_DECISION_REQUIRED", "tool": action.tool_name})
                    no_progress_count = await solver_state_service.record_progress(
                        session, run.id, False
                    )
                    await event_service.append(
                        session,
                        run.id,
                        "agent.no_progress",
                        {"tool": action.tool_name, "no_progress_count": no_progress_count},
                    )
                    if await self._stop_if_no_progress(
                        session, run, challenge, no_progress_count
                    ):
                        return
                    await self._transition(session, run, RunStatus.PLANNING)
                    continue
                hypothesis = (
                    action.hypothesis.statement
                    if isinstance(action.hypothesis, ActionHypothesis)
                    else str(action.hypothesis)
                )
                hypothesis_item, created = await hypothesis_service.upsert_from_action(
                    session,
                    run.id,
                    phase=getattr(action, "phase", None),
                    objective=getattr(action, "objective", None),
                    hypothesis_text=hypothesis,
                    evidence={
                        "expected_evidence": getattr(action, "expected_evidence", None),
                        "success_condition": getattr(action, "success_condition", None),
                        "failure_pivot": getattr(action, "failure_pivot", None),
                        "retry_reason": getattr(action, "retry_reason", None),
                        "tool_name": action.tool_name,
                    },
                )
                await event_service.append(
                    session,
                    run.id,
                    "agent.hypothesis_created" if created else "agent.hypothesis_updated",
                    {
                        "hypothesis_id": hypothesis_item.id,
                        "title": hypothesis_item.title,
                        "status": hypothesis_item.status,
                        "confidence": hypothesis_item.confidence,
                    },
                )
            if isinstance(action, ToolAction):
                if action.tool_name not in load_tool_definitions():
                    await event_service.append(
                        session,
                        run.id,
                        "agent.action_rejected",
                        {"tool": action.tool_name, "code": "TOOL_NOT_AVAILABLE"},
                    )
                    await solver_state_service.record_rejected_path(
                        session,
                        run.id,
                        {"tool": action.tool_name, "code": "TOOL_NOT_AVAILABLE"},
                    )
                    no_progress_count = await solver_state_service.record_progress(
                        session, run.id, False
                    )
                    await event_service.append(
                        session,
                        run.id,
                        "agent.no_progress",
                        {"tool": action.tool_name, "no_progress_count": no_progress_count},
                    )
                    if await self._stop_if_no_progress(
                        session, run, challenge, no_progress_count
                    ):
                        return
                    continue
                fingerprint = fingerprint_action(action.tool_name, action.arguments)
                state = await solver_state_service.load(session, run.id)
                fingerprint_state = (state.action_fingerprints_json if state else {}).get(fingerprint)
                if fingerprint_state and not action.retry_reason:
                    await event_service.append(
                        session,
                        run.id,
                        "agent.action_rejected",
                        {"tool": action.tool_name, "code": "DUPLICATE_ACTION"},
                    )
                    await solver_state_service.record_rejected_path(
                        session,
                        run.id,
                        {
                            "tool": action.tool_name,
                            "fingerprint": fingerprint,
                            "reason": "Duplicate action without retry reason",
                        },
                    )
                    no_progress_count = await solver_state_service.record_progress(
                        session, run.id, False
                    )
                    await event_service.append(
                        session,
                        run.id,
                        "agent.no_progress",
                        {"tool": action.tool_name, "no_progress_count": no_progress_count},
                    )
                    if no_progress_count >= 2:
                        await event_service.append(
                            session,
                            run.id,
                            "agent.replan_required",
                            {"reason": "Repeated no-progress actions"},
                        )
                    if await self._stop_if_no_progress(
                        session, run, challenge, no_progress_count
                    ):
                        return
                    continue
                if action.activate_skill:
                    if await solver_state_service.activate_skill(session, run.id, action.activate_skill):
                        await event_service.append(
                            session,
                            run.id,
                            "skill.activated",
                            {"skill_id": action.activate_skill, "source": "action"},
                        )
                if run.tool_call_count >= run.max_tool_calls:
                    await event_service.append(
                        session,
                        run.id,
                        "agent.action_rejected",
                        {"tool": action.tool_name, "code": "MAX_TOOL_CALLS"},
                    )
                    await solver_state_service.record_rejected_path(
                        session,
                        run.id,
                        {"tool": action.tool_name, "code": "MAX_TOOL_CALLS"},
                    )
                    no_progress_count = await solver_state_service.record_progress(
                        session, run.id, False
                    )
                    await event_service.append(
                        session,
                        run.id,
                        "agent.no_progress",
                        {"tool": action.tool_name, "no_progress_count": no_progress_count},
                    )
                    if await self._stop_if_no_progress(
                        session, run, challenge, no_progress_count
                    ):
                        return
                    break
                await self._transition(session, run, RunStatus.EXECUTING)
                run.tool_call_count += 1
                await session.commit()
                try:
                    result = await tool_gateway.invoke(
                        session, run, challenge, action.tool_name, action.arguments
                    )
                except DomainError as error:
                    await event_service.append(
                        session,
                        run.id,
                        "agent.action_rejected",
                        {"tool": action.tool_name, "code": error.code, "error": error.message, "details": error.details, "retryable": error.code in {"CODEX_DIRECT_TOOL_FORBIDDEN", "TOOL_INVALID_ARGUMENT", "FILE_NOT_FOUND", "SCRIPT_NOT_SYNCED", "SKILL_NOT_FOUND", "RUN_TOOL_NOT_ALLOWED", "TOOL_NOT_INSTALLED"}},
                    )
                    await solver_state_service.record_rejected_path(
                        session,
                        run.id,
                        {
                            "tool": action.tool_name,
                            "fingerprint": fingerprint,
                            "reason": error.message,
                            "code": error.code,
                        },
                    )
                    no_progress_count = await solver_state_service.record_progress(
                        session, run.id, False
                    )
                    await event_service.append(
                        session,
                        run.id,
                        "agent.no_progress",
                        {"tool": action.tool_name, "no_progress_count": no_progress_count},
                    )
                    await solver_state_service.record_fingerprint(
                        session,
                        run.id,
                        fingerprint,
                        tool_name=action.tool_name,
                        arguments=action.arguments,
                        status="REJECTED",
                        retry_reason=action.retry_reason,
                    )
                    if no_progress_count >= 2:
                        await event_service.append(
                            session,
                            run.id,
                            "agent.replan_required",
                            {"reason": "Repeated no-progress actions"},
                        )
                    if await self._stop_if_no_progress(
                        session, run, challenge, no_progress_count
                    ):
                        return
                    await self._transition(session, run, RunStatus.EVALUATING)
                    continue
                call = await session.scalar(
                    select(ToolCall)
                    .where(ToolCall.run_id == run.id, ToolCall.tool_name == action.tool_name)
                    .order_by(ToolCall.created_at.desc())
                )
                observation = None
                artifact = None
                if call:
                    observation = await session.scalar(
                        select(Observation)
                        .where(Observation.tool_call_id == call.id)
                        .order_by(Observation.created_at.desc())
                    )
                    artifact = await session.scalar(
                        select(Artifact)
                        .where(Artifact.tool_call_id == call.id)
                        .order_by(Artifact.created_at.desc())
                    )
                progress = {"made_progress": False, "no_progress_count": 0, "recommended_skills": []}
                await solver_state_service.record_experiment(
                    session,
                    run.id,
                    {
                        "question": action.objective,
                        "hypothesis": hypothesis,
                        "positive_signal": action.success_condition,
                        "negative_signal": action.failure_pivot,
                        "tool": action.tool_name,
                        "arguments": action.arguments,
                        "result_classification": "COMPLETED" if result.get("status") == "COMPLETED" else "ERROR",
                        "new_facts": (result.get("structured_result") or {}).get("extracted_facts", {}) if isinstance(result.get("structured_result"), dict) else {},
                        "capability_change": [],
                        "next_decision": action.failure_pivot if result.get("status") != "COMPLETED" else action.success_condition,
                    },
                )
                if call and observation and artifact:
                    progress = await progress_evaluator.evaluate(
                        session,
                        run,
                        challenge,
                        action.arguments,
                        action.tool_name,
                        result,
                        observation,
                        artifact,
                    )
                await solver_state_service.record_fingerprint(
                    session,
                    run.id,
                    fingerprint,
                    tool_name=action.tool_name,
                    arguments=action.arguments,
                    status=str(result.get("status") or "UNKNOWN"),
                    retry_reason=action.retry_reason,
                )
                if hypothesis_item:
                    await hypothesis_service.mark_result(
                        session,
                        hypothesis_item.id,
                        result_status=str(result.get("status") or "UNKNOWN"),
                        observation=observation.facts_json if observation else None,
                        evidence={"tool_name": action.tool_name, "status": result.get("status")},
                    )
                if progress["made_progress"]:
                    capability_by_tool = {
                        "http_request": "can_read_public_page",
                        "http_session_request": "can_reuse_session",
                        "file_read": "can_read_file",
                        "script_run": "can_run_script",
                        "python_run": "can_run_script",
                        "jwt_inspect": "can_forge_token",
                    }
                    capability = capability_by_tool.get(action.tool_name)
                    if capability:
                        await solver_state_service.record_capability(session, run.id, capability, evidence={"tool": action.tool_name})
                    for recommendation in progress["recommended_skills"]:
                        await event_service.append(
                            session,
                            run.id,
                            "skill.recommended",
                            {
                                "skill_id": recommendation["skill_id"],
                                "skill_name": recommendation["skill_name"],
                                "matched_triggers": recommendation.get(
                                    "matched_positive_triggers",
                                    recommendation.get("matched_triggers", []),
                                ),
                                "confidence": recommendation["confidence"],
                                "source": "observation",
                            },
                        )
                    await event_service.append(
                        session,
                        run.id,
                        "agent.progress_detected",
                        {
                            "tool": action.tool_name,
                            "no_progress_count": progress["no_progress_count"],
                            "recommended_skills": progress["recommended_skills"],
                        },
                    )
                else:
                    await event_service.append(
                        session,
                        run.id,
                        "agent.no_progress",
                        {
                            "tool": action.tool_name,
                            "no_progress_count": progress["no_progress_count"],
                        },
                    )
                await event_service.append(
                    session,
                    run.id,
                    "agent.action_completed",
                    {"type": "tool", "tool": action.tool_name, "status": result.get("status")},
                )
                if progress["no_progress_count"] >= 2:
                    await event_service.append(
                        session,
                        run.id,
                        "agent.replan_required",
                        {"reason": "Repeated no-progress actions"},
                    )
                if progress["no_progress_count"] >= 6:
                    await self._transition(session, run, RunStatus.REPORTING)
                    await report_service.generate(
                        session,
                        run,
                        challenge,
                        "unsolved",
                        "连续 6 次动作没有产生新的结构化证据，任务受控结束。",
                    )
                    await self._transition(session, run, RunStatus.COMPLETED_UNSOLVED)
                    return
                if result.get("status") == "COMPLETED":
                    consecutive_runner_failures = 0
                    last_runner_failure = None
                else:
                    failure = (
                        action.tool_name,
                        str(result.get("error") or result.get("summary") or "Runner execution failed"),
                    )
                    consecutive_runner_failures = (
                        consecutive_runner_failures + 1
                        if failure == last_runner_failure
                        else 1
                    )
                    last_runner_failure = failure
                    recoverable_codes = {"CODEX_DIRECT_TOOL_FORBIDDEN", "TOOL_INVALID_ARGUMENT", "FILE_NOT_FOUND", "SCRIPT_NOT_SYNCED", "SKILL_NOT_FOUND", "RUN_TOOL_NOT_ALLOWED", "TOOL_NOT_INSTALLED", "SCRIPT_TIMEOUT", "TOOL_NOT_INSTALLED"}
                    if str(result.get("error_code") or "") in recoverable_codes:
                        await event_service.append(session, run.id, "tool.rejected", {"tool": action.tool_name, "code": result.get("error_code"), "error": failure[1], "retryable": True})
                        consecutive_runner_failures = 0
                        last_runner_failure = None
                    elif consecutive_runner_failures >= 2:
                        run.last_error_code = "RUNNER_UNAVAILABLE"
                        run.last_error_message = failure[1][:4000]
                        await self._transition(session, run, RunStatus.FAILED_RUNNER)
                        await event_service.append(
                            session,
                            run.id,
                            "run.failed",
                            {"code": "RUNNER_UNAVAILABLE", "message": failure[1][:1000]},
                        )
                        return
                await self._transition(session, run, RunStatus.EVALUATING)
                continue
            finished = await self._finish(session, run, challenge, action)
            if finished:
                return
            continue
        if RunStatus(run.status) not in TERMINAL:
            await self._transition(session, run, RunStatus.REPORTING)
            await report_service.generate(
                session, run, challenge, "unsolved", "Maximum agent steps or tool calls reached"
            )
            await self._transition(session, run, RunStatus.COMPLETED_UNSOLVED)

    async def _finish(
        self, session, run: SolveRun, challenge: Challenge, action: FinishAction
    ) -> bool:
        if action.result == "waiting_user":
            await self._transition(session, run, RunStatus.WAITING_USER)
            await event_service.append(
                session,
                run.id,
                "agent.action_completed",
                {"type": "finish", "result": "waiting_user"},
            )
            return True
        if action.result == "unsolved" and run.engine_type == "openai_compatible":
            tool_count = await session.scalar(select(func.count()).select_from(ToolCall).where(ToolCall.run_id == run.id))
            observation_count = await session.scalar(select(func.count()).select_from(Observation).where(Observation.run_id == run.id))
            state = await solver_state_service.load(session, run.id)
            directions = {str(item.get("source") or item.get("tool") or "") for item in ((state.confirmed_facts_json if state else []) + (state.rejected_paths_json if state else []))}
            blockers = {"TARGET_UNREACHABLE", "ATTACHMENT_MISSING", "RUNNER_UNAVAILABLE", "PROVIDER_CONFIGURATION_INVALID", "AUTHORIZATION_BOUNDARY_UNCLEAR"}
            if not tool_count or not observation_count or (len(directions) < 2 and str(run.last_error_code or "") not in blockers):
                missing = []
                if not tool_count: missing.append("at least one tool call")
                if not observation_count: missing.append("at least one valid observation")
                if len(directions) < 2: missing.append("two independently tested directions or an explicit blocker")
                message = "FINISH_PREMATURE: " + ", ".join(missing)
                await event_service.append(session, run.id, "agent.action_rejected", {"type": "finish", "code": "FINISH_PREMATURE", "message": message, "missing": missing})
                await solver_state_service.record_rejected_path(session, run.id, {"source": "finish_gate", "code": "FINISH_PREMATURE", "missing": missing})
                await self._transition(session, run, RunStatus.PLANNING)
                return False
        solved = False
        if action.flag_candidate:
            await self._transition(session, run, RunStatus.VERIFYING_FLAG)
            solved = await flag_service.verify(session, run, challenge, action.flag_candidate)
        allowed, code, message = await finish_gate.evaluate(
            session, run, challenge, candidate_verified=solved
        )
        if not allowed:
            await event_service.append(
                session,
                run.id,
                "agent.action_rejected",
                {"type": "finish", "code": code, "message": message},
            )
            await solver_state_service.record_rejected_path(
                session,
                run.id,
                {"source": "finish_gate", "code": code, "message": message},
            )
            await self._transition(session, run, RunStatus.PLANNING)
            return False
        await self._transition(session, run, RunStatus.REPORTING)
        result = "solved" if action.result == "solved" and solved else "unsolved"
        await report_service.generate(
            session,
            run,
            challenge,
            result,
            "Flag did not match the configured pattern"
            if action.result == "solved" and not solved
            else "",
        )
        await event_service.append(
            session, run.id, "agent.action_completed", {"type": "finish", "result": result}
        )
        await self._transition(
            session,
            run,
            RunStatus.COMPLETED_SOLVED if result == "solved" else RunStatus.COMPLETED_UNSOLVED,
        )
        with contextlib.suppress(Exception):
            await runner_client.clear_sessions(run.id)
        return True

    async def _run_event_engine(
        self,
        session,
        run: SolveRun,
        engine: SolveEngine,
        user_message: str | None,
        attempt=None,
        lease=None,
    ) -> None:
        queued_input = await self._consume_queued_inputs(session, run, attempt)
        user_message = "\n\n".join(item for item in (user_message, queued_input) if item) or None
        if run.status == RunStatus.CREATED:
            await self._transition(session, run, RunStatus.PREPARING)
            await event_service.append(session, run.id, "run.started", {})
            iterator = engine.start(run.id)
        elif user_message:
            if run.status in {RunStatus.WAITING_USER, RunStatus.PAUSED_CHECKPOINT, RunStatus.PAUSED_RECOVERY, RunStatus.WAITING_CONFIGURATION, RunStatus.PAUSED_RATE_LIMIT}:
                await self._transition(session, run, RunStatus.PLANNING)
            iterator = engine.continue_run(run.id, user_message)
        else:
            if run.status in {RunStatus.WAITING_USER, RunStatus.PAUSED_CHECKPOINT, RunStatus.PAUSED_RECOVERY, RunStatus.WAITING_CONFIGURATION, RunStatus.PAUSED_RATE_LIMIT}:
                await self._transition(session, run, RunStatus.PLANNING)
            iterator = engine.resume(run.id)
        auto_turns = 0
        max_auto_turns = max(1, run.max_agent_steps)
        auto_started = monotonic()
        no_progress_turns = 0
        while True:
            await run_attempt_service.heartbeat(session, attempt, lease)
            auto_turns += 1
            before_progress = await self._codex_progress_snapshot(session, run.id)
            async for item in iterator:
                thread_id = item.payload.get("thread_id")
                if isinstance(thread_id, str):
                    run.codex_thread_id = thread_id
                    await session.commit()
                if item.status and item.status != run.status:
                    await self._transition(session, run, RunStatus(item.status))
                await event_service.append(session, run.id, item.event_type, item.payload)
            await codex_materializer.sync(session, run)
            interval = max(1, int(run.agent_checkpoint_interval or 30))
            if run.agent_step_count and run.agent_step_count % interval == 0:
                await self._transition(session, run, RunStatus.PAUSED_CHECKPOINT)
                await event_service.append(session, run.id, "run.checkpoint_reached", {"step": run.agent_step_count, "interval": interval, "phase": run.current_phase, "remaining_steps": max(0, run.max_agent_steps - run.agent_step_count)})
                return
            after_progress = await self._codex_progress_snapshot(session, run.id)
            if after_progress == before_progress:
                no_progress_turns += 1
            else:
                no_progress_turns = 0
            current_status = RunStatus(run.status)
            if current_status in TERMINAL or current_status == RunStatus.WAITING_USER:
                return
            if run.engine_type != "codex_sdk":
                return
            if auto_turns >= max_auto_turns:
                await self._transition(session, run, RunStatus.WAITING_USER)
                await event_service.append(
                    session,
                    run.id,
                    "agent.message",
                    {
                        "message": "自动续跑已达到本任务的轮次上限，请补充信息后继续。",
                        "requires_user_confirmation": True,
                        "reason": "AUTO_TURN_LIMIT",
                    },
                )
                return
            if no_progress_turns >= 8:
                await event_service.append(
                    session,
                    run.id,
                    "agent.no_progress_diagnostic",
                    {
                        "message": "Codex 连续多轮未产生结构化进展，已记录内部诊断并继续尝试不同维度。",
                        "reason": "CODEX_NO_PROGRESS",
                        "no_progress_turns": no_progress_turns,
                    },
                )
                no_progress_turns = 0
            if monotonic() - auto_started >= run.max_runtime_seconds:
                await self._transition(session, run, RunStatus.WAITING_USER)
                await event_service.append(
                    session,
                    run.id,
                    "agent.message",
                    {
                        "message": "自动续跑已达到本任务的运行时长上限，请补充信息后继续。",
                        "requires_user_confirmation": True,
                        "reason": "AUTO_RUNTIME_LIMIT",
                    },
                )
                return
            queued_input = await self._consume_queued_inputs(session, run, attempt)
            iterator = engine.continue_run(run.id, queued_input) if queued_input else engine.resume(run.id)

    async def _codex_progress_snapshot(
        self, session, run_id: str
    ) -> tuple[int, int, int, int, str | None]:
        # Do not count raw RunEvent rows here.  Codex mock mode and some SDK
        # failures can emit fresh agent.message/turn.completed rows forever
        # without producing any usable evidence.  Only durable solving outputs
        # or a status change count as meaningful progress for auto-resume.
        tool_count = await session.scalar(
            select(func.count()).select_from(ToolCall).where(ToolCall.run_id == run_id)
        )
        artifact_count = await session.scalar(
            select(func.count()).select_from(Artifact).where(Artifact.run_id == run_id)
        )
        observation_count = await session.scalar(
            select(func.count()).select_from(Observation).where(Observation.run_id == run_id)
        )
        flag_count = await session.scalar(
            select(func.count()).select_from(FlagCandidate).where(FlagCandidate.run_id == run_id)
        )
        status = await session.scalar(select(SolveRun.status).where(SolveRun.id == run_id))
        return (
            int(tool_count or 0),
            int(artifact_count or 0),
            int(observation_count or 0),
            int(flag_count or 0),
            status,
        )

    async def continue_with_message(self, run_id: str, message: str) -> None:
        await self.start(run_id, message)

    async def cancel(self, run_id: str) -> None:
        engine = self.active_engines.get(run_id)
        if isinstance(engine, SolveEngine):
            await engine.cancel(run_id)
        task = self.active_tasks.get(run_id)
        if task and task is not asyncio.current_task():
            task.cancel()


orchestrator = SolveOrchestrator()
