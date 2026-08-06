import asyncio
import contextlib
import hashlib
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.exceptions import DomainError
from app.engines import (
    BridgeConfigurationError,
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
    RunAttempt,
    RunEvent,
    RunExecutionLease,
    RunUserInput,
    SolveRun,
    ToolCall,
)
from app.models.skill import RunSkillSnapshot, Skill
from app.orchestration.state_machine import SOLVER_PHASES, TERMINAL, RunStatus, transition
from app.schemas.agent import (
    ActionHypothesis,
    AutomationAction,
    FinishAction,
    PlanAction,
    SkillAction,
    ToolAction,
)
from app.services.action_fingerprint import fingerprint_action
from app.services.action_quality import action_quality_gate, recovery_planner
from app.services.attack_chain import classify_rejection
from app.services.automation_policy import automation_policy_engine
from app.services.codex_materializer import codex_materializer, logical_tool_budget_ref
from app.services.codex_preflight import codex_preflight_service
from app.services.context_builder import context_builder
from app.services.crypto import decrypt_api_key
from app.services.events import event_service
from app.services.user_input_consumer import consume_user_inputs
from app.services.evidence_pipeline import evidence_pipeline
from app.services.finish_gate import finish_gate
from app.services.flags import flag_service
from app.services.fresh_reproduction import fresh_reproduction_executor
from app.services.hypotheses import hypothesis_service
from app.services.infrastructure import clear_failure, infrastructure_error, record_failure
from app.services.progress_evaluator import progress_evaluator
from app.services.reports import ReproductionPlanner, report_service
from app.services.run_attempts import run_attempt_service
from app.services.run_finalizer import run_finalizer
from app.services.runner_client import runner_client
from app.services.script_controller import script_fallback_controller
from app.services.solver_state import solver_state_service
from app.services.tool_manifest import refresh_runtime_tool_manifest
from app.services.tool_permissions import effective_tools_for
from app.orchestration.multi_agent_orchestrator import multi_agent_orchestrator
from app.services.run_budget_guard import run_budget_guard
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


def _tool_rate_limit_status(payload: dict) -> int | None:
    """Return an HTTP status exposed by a tool result, if it is a rate limit."""
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        result = {}
    model_view = result.get("model_view")
    if not isinstance(model_view, dict):
        model_view = {}
    facts = model_view.get("extracted_facts")
    if not isinstance(facts, dict):
        facts = {}
    candidates = (facts.get("status_code"), result.get("status_code"), payload.get("status_code"))
    for candidate in candidates:
        try:
            status = int(candidate)
        except (TypeError, ValueError):
            continue
        if status == 429:
            return status
    return None


class SolveOrchestrator:
    def __init__(self, engine_factory=None) -> None:
        self.engine_factory = engine_factory
        self.active_engines: dict[str, object] = {}
        self.active_tasks: dict[str, asyncio.Task[None]] = {}

    async def _lease_heartbeat_loop(self, run_id: str, attempt_id: str, lease_id: str) -> None:
        while True:
            await asyncio.sleep(15)
            async with SessionLocal() as heartbeat_session:
                attempt = await heartbeat_session.get(RunAttempt, attempt_id)
                lease = await heartbeat_session.get(RunExecutionLease, lease_id)
                if not attempt or not lease or attempt.run_id != run_id or attempt.status != "RUNNING":
                    return
                now = datetime.now(UTC)
                attempt.heartbeat_at = now
                lease.heartbeat_at = now
                lease.expires_at = now + timedelta(seconds=run_attempt_service.lease_ttl_seconds)
                await heartbeat_session.commit()

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
        await event_service.append(session, run.id, "run.phase_changed", {"previous_phase": previous, "phase": phase, "source": "model_action"})

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
                # MCP credentials are bound to the Attempt/Lease at thread
                # creation. A persisted thread belongs to an older attempt
                # and must never be resumed with a new lease.
                thread_id=None,
                scope={
                    "run_id": run.id,
                    "challenge_id": run.challenge_id,
                    "workspace_root": run.workspace_path,
                    "allowed_hosts": list(challenge.allowed_hosts or []) if challenge else [],
                    "attempt_id": attempt.id if attempt else None,
                    "lease_token": lease.lease_token if lease else None,
                    "master_lease_token": lease.lease_token if lease else None,
                    # multi_agent_v1 owns the tool loop in the backend.  The
                    # role model only emits a contract/RoleAction and must
                    # never depend on an MCP catalog or preflight cache.
                    "execution_mode": "controller_tool_loop",
                    "mcp_enabled": False,
                    "mcp_required": False,
                    "recovery_checkpoint": run.recovery_checkpoint_json or {},
                    "available_tools": [],
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
        previous_phase = str(run.current_phase or "")
        transition(run, target)
        await session.commit()
        await solver_state_service.sync_from_run(session, run)
        await event_service.append(session, run.id, "run.status_changed", {"status": run.status})
        current_phase = str(run.current_phase or "")
        if previous_phase != current_phase and previous_phase in SOLVER_PHASES and current_phase in SOLVER_PHASES:
            await event_service.append(session, run.id, "run.phase_changed", {"previous_phase": previous_phase, "phase": current_phase})

    async def _consume_queued_inputs(self, session, run: SolveRun, attempt=None) -> str | None:
        consumed = await consume_user_inputs(session, run, attempt)
        if consumed["items"]:
            await event_service.append(session, run.id, "run.guidance_updated", {"context_revision": run.context_revision})
            return consumed["text"]
        return None

    async def _stop_if_no_progress(
        self,
        session,
        run: SolveRun,
        challenge: Challenge,
        no_progress_count: int,
    ) -> bool:
        return await self._recover_no_progress(session, run, challenge, no_progress_count)
    async def _recover_no_progress(self, session, run: SolveRun, challenge: Challenge, no_progress_count: int) -> bool:
        if no_progress_count < 2:
            return False
        state = await solver_state_service.load(session, run.id)
        ledger = state.capability_ledger_json if state else {}
        boolean_confirmed = any(
            key in ledger for key in ("matched_boolean_oracle_confirmed", "boolean_oracle_confirmed")
        )
        flag_verified = bool(
            await session.scalar(
                select(FlagCandidate.id).where(
                    FlagCandidate.run_id == run.id,
                    FlagCandidate.verified.is_(True),
                    FlagCandidate.review_state == "VALID",
                )
            )
        )
        if boolean_confirmed and not flag_verified:
            if no_progress_count >= 2:
                run.recovery_checkpoint_json = {**(run.recovery_checkpoint_json or {}), "force_script_fallback": True}
                await session.commit()
                attempt = await session.scalar(select(RunAttempt).where(RunAttempt.run_id == run.id, RunAttempt.status == "RUNNING"))
                lease = await session.scalar(select(RunExecutionLease).where(RunExecutionLease.run_id == run.id))
                if attempt and lease:
                    fallback = await script_fallback_controller.run(session, run, challenge, attempt, lease)
                    if fallback.get("status") in {"COMPLETED", "PARTIAL", "FAILED"}:
                        await event_service.append(session, run.id, "agent.extraction_controller_result", {"status": fallback.get("status"), "script_id": fallback.get("script_id"), "error_code": fallback.get("error_code")})
                        if fallback.get("status") in {"COMPLETED", "PARTIAL"}:
                            return False
            if no_progress_count == 1:
                await event_service.append(
                    session,
                    run.id,
                    "agent.required_action",
                    {
                        "required_action": "BOUNDED_EXTRACTION",
                        "preferred_tools": ["boolean_config_extract", "script_run"],
                        "reason": "boolean_oracle_confirmed_without_verified_flag",
                    },
                )
            elif no_progress_count == 2:
                await solver_state_service.require_plan_action(session, run.id, True)
                await event_service.append(
                    session,
                    run.id,
                    "agent.extraction_task_forced",
                    {"tool": "boolean_config_extract", "fallback": "script_run"},
                )
            elif no_progress_count >= 3:
                run.last_error_code = "METHOD_ACTION_REQUIRED"
                run.last_error_message = "A confirmed boolean oracle produced three replans without a durable extraction action."
                await self._transition(session, run, RunStatus.PAUSED_CHECKPOINT)
                await event_service.append(
                    session,
                    run.id,
                    "agent.recovery_checkpoint",
                    {"code": "METHOD_ACTION_REQUIRED", "current_phase": run.current_phase, "next_required_action": "BOUNDED_EXTRACTION"},
                )
                return True
        stages = {
            2: ("RECLASSIFY_REPLAN", "重新分类当前结果并重规划"),
            4: ("SWITCH_TOOL_DIMENSION", "切换工具维度"),
            6: ("ABANDON_HYPOTHESIS", "放弃当前假设并选择新的攻击链节点"),
            8: ("RECOVERY_PLANNER", "进入恢复规划器"),
        }
        stage = stages.get(no_progress_count)
        if stage:
            await event_service.append(session, run.id, "agent.recovery_stage", {"stage": stage[0], "no_progress_count": no_progress_count, "message": stage[1]})
            if no_progress_count >= 6:
                await solver_state_service.require_plan_action(session, run.id, True)
        if no_progress_count >= 12:
            await self._transition(session, run, RunStatus.PAUSED_RECOVERY)
            await event_service.append(session, run.id, "agent.recovery_checkpoint", {"no_progress_count": no_progress_count, "requires_user_input": True, "message": "恢复规划已达到 12 次连续无进展，请补充方向或配置后继续。"})
            return True
        return False

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
        existing = self.active_tasks.get(run_id)
        if existing is not None and existing is not task and not existing.done():
            # A supervisor wake-up may race with the original controller task
            # before the durable production ToolCall exists.  The original
            # task owns this Run; a second start must not create another
            # Planner/Proposal chain.
            return
        if task:
            self.active_tasks[run_id] = task
        async with SessionLocal() as session:
            attempt = None
            lease = None
            heartbeat_task = None
            try:
                run = await session.scalar(select(SolveRun).where(SolveRun.id == run_id))
                if not run:
                    return
                await run_finalizer.reconcile(session, run)
                if run.engine_type == "codex_sdk" and run.solver_mode != "multi_agent_v1":
                    await solver_state_service.ensure_confirmed_boolean_checkpoint(session, run)
                try:
                    existing_lease = await session.scalar(
                        select(RunExecutionLease).where(RunExecutionLease.run_id == run.id)
                    )
                    if existing_lease is not None and existing_lease.owner_instance_id == run_attempt_service.owner_instance_id:
                        lease = existing_lease
                        attempt = await session.get(RunAttempt, lease.attempt_id)
                        if attempt is None:
                            raise DomainError("RUN_ATTEMPT_MISSING", "Active execution lease has no attempt.")
                    else:
                        attempt, lease = await run_attempt_service.begin(session, run)
                    challenge_for_manifest = await session.get(Challenge, run.challenge_id)
                    if challenge_for_manifest is not None:
                        manifest = await refresh_runtime_tool_manifest(
                            session,
                            run,
                            attempt,
                            challenge_for_manifest,
                            # Controller-owned loop: effective tools come from
                            # role policy, challenge policy, backend registry
                            # and Runner capability only.
                            mcp_tools=[],
                        )
                        await session.commit()
                        await event_service.append(
                            session,
                            run.id,
                            "attempt.tool_manifest_refreshed",
                            {"manifest_id": manifest.id, "manifest_sha256": manifest.manifest_sha256, "missing_expected_tools": manifest.missing_expected_tools},
                        )
                        if attempt.tool_manifest_status == "DRIFT" and run.engine_type == "codex_sdk":
                            await event_service.append(session, run.id, "run.configuration_blocked", {"code": "TOOL_CATALOG_DRIFT", "missing_expected_tools": manifest.missing_expected_tools, "action": "restart_backend_runner_bridge"})
                            return
                    heartbeat_task = asyncio.create_task(self._lease_heartbeat_loop(run.id, attempt.id, lease.id))
                    if run.status == RunStatus.PAUSED_DEPLOYMENT:
                        run.last_error_code = None
                        run.last_error_message = None
                        await session.commit()
                        await event_service.append(session, run.id, "run.resumed_after_deployment", {"code": "RESUMED_AFTER_DEPLOYMENT"})
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
                if run.solver_mode == "multi_agent_v1":
                    await multi_agent_orchestrator.run(session, run, await session.get(Challenge, run.challenge_id), attempt, lease, engine=engine)
                elif run.engine_type == "openai_compatible":
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
                    elif isinstance(error, BridgeConfigurationError):
                        code = error.code
                    elif isinstance(error, BridgeUnavailableError):
                        code = "CODEX_STREAM_INTERRUPTED"
                    elif isinstance(error, DomainError):
                        code = error.code
                    else:
                        code = "ENGINE_ERROR"
                    run.last_error_code, run.last_error_message = code, str(error)[:4000]
                    target = RunStatus.PAUSED_RATE_LIMIT if isinstance(error, ModelRateLimitError) else RunStatus.PAUSED_RECOVERY if isinstance(error, BridgeUnavailableError) or code == "CODEX_STREAM_INTERRUPTED" else RunStatus.WAITING_CONFIGURATION if isinstance(error, (BridgeConfigurationError, DomainError) and code == "RUN_CONFIGURATION_BLOCKED") else RunStatus.FAILED_ENGINE
                    await self._transition(session, run, target)
                    await event_service.append(
                        session,
                        run_id,
                        "run.paused_recovery" if isinstance(error, BridgeUnavailableError) or code == "CODEX_STREAM_INTERRUPTED" else "run.configuration_blocked" if isinstance(error, BridgeConfigurationError) or code == "RUN_CONFIGURATION_BLOCKED" else "run.failed",
                        {"code": code, "message": str(error)[:1000]},
                    )
            finally:
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await heartbeat_task
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
            RunStatus.PAUSED_DEPLOYMENT,
            RunStatus.WAITING_CONFIGURATION,
        }:
            await self._transition(session, run, RunStatus.PLANNING)
        else:
            raise DomainError("RUN_INVALID_STATE", "Run cannot be started from its current state.")
        challenge = await session.get(Challenge, run.challenge_id)
        if not challenge:
            raise ValueError("challenge not found")
        state = await solver_state_service.load(session, run.id)
        await solver_state_service.initialize(
            session,
            run,
            challenge.challenge_type,
            list(state.active_skill_ids_json or []) if state else [],
            challenge.name,
            challenge.description,
        )
        started = monotonic()
        consecutive_runner_failures = 0
        last_runner_failure: tuple[str, str] | None = None
        while run.run_total_agent_steps < run.max_agent_steps:
            await run_attempt_service.heartbeat(session, attempt, lease)
            if RunStatus(run.status) == RunStatus.CANCELLED:
                return
            if monotonic() - started > run.max_runtime_seconds:
                await self._transition(session, run, RunStatus.TIMEOUT)
                return
            if run.started_at:
                started_at = run.started_at.replace(tzinfo=UTC) if run.started_at.tzinfo is None else run.started_at
                if (datetime.now(UTC) - started_at).total_seconds() > run.max_total_runtime_seconds:
                    run.last_error_code = "MAX_TOTAL_RUNTIME"
                    run.last_error_message = (
                        f"Run total runtime limit reached ({run.max_total_runtime_seconds}s); restart to open a new runtime window."
                    )
                    await self._transition(session, run, RunStatus.PAUSED_CHECKPOINT)
                    await event_service.append(
                        session,
                        run.id,
                        "run.runtime_checkpoint",
                        {
                            "code": "MAX_TOTAL_RUNTIME",
                            "max_total_runtime_seconds": run.max_total_runtime_seconds,
                            "requires_user_input": True,
                        },
                    )
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
            turn_id = str(uuid.uuid4())
            turn_started_at = datetime.now(UTC)
            run.active_turn_id = turn_id
            await session.commit()
            try:
                action = await engine.next_action(messages)
            except Exception:
                # Preserve provider parse/retry telemetry even when no action
                # can be executed. This makes FAILED_ENGINE diagnosable without
                # persisting secrets or the full prompt.
                trace = getattr(engine, "last_trace", {})
                session.add(
                    AgentTurn(
                        id=turn_id,
                        run_id=run.id,
                        step_number=run.run_total_agent_steps + 1,
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
                        turn_started_at=turn_started_at,
                        turn_finished_at=datetime.now(UTC),
                    )
                )
                await session.commit()
                raise
            trace = getattr(engine, "last_trace", {})
            session.add(
                AgentTurn(
                    id=turn_id,
                    run_id=run.id,
                    step_number=run.run_total_agent_steps + 1,
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
                    turn_started_at=turn_started_at,
                    turn_finished_at=datetime.now(UTC),
                )
            )
            run.agent_step_count += 1
            run.run_total_agent_steps += 1
            run.attempt_agent_steps += 1
            run.checkpoint_segment_steps += 1
            await session.commit()
            interval = max(1, int(run.agent_checkpoint_interval or 30))
            if run.checkpoint_segment_steps >= interval:
                await self._transition(session, run, RunStatus.PAUSED_CHECKPOINT)
                await event_service.append(
                    session,
                    run.id,
                    "run.checkpoint_reached",
                    {
                        "step": run.run_total_agent_steps,
                        "attempt_step": run.attempt_agent_steps,
                        "segment_steps": run.checkpoint_segment_steps,
                        "interval": interval,
                        "phase": run.current_phase,
                        "remaining_steps": max(0, run.max_agent_steps - run.run_total_agent_steps),
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
                    "reason": getattr(action, "reason", None)
                    or getattr(action, "summary", None)
                    or getattr(action, "objective", None),
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
                    "automation_tool": getattr(action, "automation_tool", None),
                    "plan_node_id": getattr(action, "plan_node_id", None),
                    "experiment_id": getattr(action, "experiment_id", None),
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
            if isinstance(action, PlanAction):
                state = await solver_state_service.load(session, run.id)
                plan = dict(state.run_plan_json or {}) if state else {}
                plan.update(
                    {
                        "current_node_id": action.plan_node_id,
                        "current_goal": action.objective,
                        "current_experiment": {
                            "experiment_id": action.experiment_id,
                            "hypothesis_id": action.hypothesis_id,
                            "decision_question": action.decision_question,
                            "next_tool": action.next_tool,
                            "expected_evidence": action.expected_evidence,
                            "failure_pivot": action.failure_pivot,
                        },
                    }
                )
                await solver_state_service.set_run_plan(session, run.id, plan)
                await solver_state_service.require_plan_action(session, run.id, False)
                await event_service.append(
                    session,
                    run.id,
                    "agent.plan_committed",
                    {
                        "plan_node_id": action.plan_node_id,
                        "experiment_id": action.experiment_id,
                        "hypothesis_id": action.hypothesis_id,
                        "next_tool": action.next_tool,
                        "decision_question": action.decision_question,
                    },
                )
                await solver_state_service.record_progress(session, run.id, True)
                continue
            automation_context = None
            if isinstance(action, AutomationAction):
                # AutomationAction is a first-class request, not a telemetry
                # event.  Convert it only after the controller has performed
                # the bounded budget/permission gate; the normal ToolGateway
                # path then records the Runner call, artifact and observation.
                definitions = load_tool_definitions()
                if action.automation_tool not in definitions:
                    await event_service.append(
                        session, run.id, "automation.failed",
                        {"code": "TOOL_NOT_AVAILABLE", "tool": action.automation_tool, "experiment_id": action.experiment_id},
                    )
                    await solver_state_service.record_control_rejection(
                        session, run.id, {"code": "TOOL_NOT_AVAILABLE", "tool": action.automation_tool}
                    )
                    await self._transition(session, run, RunStatus.EVALUATING)
                    continue
                automation_context = action
                await event_service.append(
                    session, run.id, "automation.started",
                    {
                        "phase": action.phase,
                        "plan_node_id": action.plan_node_id,
                        "experiment_id": action.experiment_id,
                        "tool": action.automation_tool,
                        "objective": action.objective,
                        "stop_conditions": action.stop_conditions,
                        "expected_artifacts": action.expected_artifacts,
                    },
                )
                action = ToolAction(
                    type="tool",
                    phase=action.phase,
                    objective=action.objective,
                    hypothesis=ActionHypothesis(category=action.vulnerability_class.upper(), statement=action.objective, confidence=90),
                    tool_name=action.automation_tool,
                    arguments=action.arguments,
                    reason="Controller-approved bounded automation experiment",
                    expected_evidence=", ".join(action.expected_artifacts) or "A structured automation artifact",
                    success_condition=action.decision_question,
                    failure_pivot=action.failure_pivot,
                )
            state = await solver_state_service.load(session, run.id)
            if isinstance(action, ToolAction):
                quality = action_quality_gate.evaluate(
                    action.model_dump(),
                    {
                        "current_phase": state.current_phase if state else run.current_phase,
                        "confirmed_facts": state.confirmed_facts_json if state else [],
                        "plan_node_id": (state.run_plan_json or {}).get("current_node_id") if state else None,
                        "decision_question": (state.last_decision_card_json or {}).get("core_question") if state else None,
                        "degraded_action_streak": state.degraded_action_streak if state else 0,
                    },
                )
                # Once the controller has raised force_plan_action, the next
                # ToolAction is the action that triggered recovery.  Let the
                # deterministic controller repair it below; sending it back
                # through this gate first creates the old quality/plan loop.
                if quality.quality != "ACCEPT" and not (state and state.force_plan_action):
                    if state:
                        state.degraded_action_streak = quality.streak
                        if quality.action in {"PlanAction", "RecoveryPlanner"}:
                            state.force_plan_action = 1
                        await session.commit()
                    await event_service.append(session, run.id, "agent.action_rejected", {"type": "tool", "code": "ACTION_QUALITY_DEGRADED", "required_action": quality.action, "reason": quality.reason, "streak": quality.streak})
                    await self._transition(session, run, RunStatus.PLANNING)
                    continue
            if state and state.force_plan_action and isinstance(action, ToolAction):
                await event_service.append(
                    session,
                    run.id,
                    "agent.action_rejected",
                    {"type": "tool", "code": "PLAN_REQUIRED", "missing": ["explicit PlanAction after repeated premature finishes"]},
                )
                await solver_state_service.record_control_rejection(
                    session,
                    run.id,
                    {"code": "PLAN_REQUIRED", "tool": action.tool_name},
                )
                current_state = await solver_state_service.load(session, run.id)
                # Do not let a provider that keeps returning ToolAction spin
                # forever. Materialize a bounded recovery plan from the
                # rejected action, then ask the model for the next action with
                # the plan requirement satisfied. Mark it as controller
                # generated in the audit trail.
                hypothesis = (
                    action.hypothesis.statement
                    if isinstance(action.hypothesis, ActionHypothesis)
                    else str(action.hypothesis)
                )
                recovery_id = f"controller-recovery-{run.run_total_agent_steps}"
                plan = dict(current_state.run_plan_json or {}) if current_state else {}
                vulnerability_class = "sql_injection" if any(
                    str(item).lower() in {"sql_injection_confirmed", "sql_syntax_signal"}
                    for item in (current_state.capability_ledger_json or {}).keys()
                ) else ""
                recovery_tool = automation_policy_engine.recovery_tool(vulnerability_class, action.tool_name)
                plan.update(
                    {
                        "current_node_id": recovery_id,
                        "current_goal": action.objective,
                        "current_experiment": {
                            "experiment_id": f"{recovery_id}-experiment",
                            "hypothesis_id": action.hypothesis_id,
                            "decision_question": action.success_condition or action.reason,
                            "next_tool": recovery_tool,
                            "expected_evidence": action.expected_evidence,
                            "failure_pivot": action.failure_pivot,
                            "hypothesis": hypothesis,
                        },
                        "source": "controller_recovery",
                    }
                )
                await solver_state_service.set_run_plan(session, run.id, plan)
                await solver_state_service.require_plan_action(session, run.id, False)
                await event_service.append(
                    session,
                    run.id,
                    "agent.plan_created",
                    {
                        "source": "controller_recovery",
                        "plan_node_id": recovery_id,
                        "next_tool": recovery_tool,
                        "decision_question": action.success_condition or action.reason,
                        "automation_required": recovery_tool not in {"file_read", "http_request"},
                    },
                )
                await solver_state_service.record_result_classification(session, run.id, "CONTROL_REJECTION")
                await solver_state_service.require_plan_action(session, run.id, False)
                # The Controller has now supplied the missing plan. Repair
                # the current model action in-place and execute it in this
                # Attempt; a second model turn must not be required merely to
                # repeat the same action with a different wrapper.
                action.reason = "Controller-repaired action under a committed recovery plan"
                action.retry_reason = "controller_recovery_plan"
                action.objective = action.objective or "Continue the authorized investigation"
                state = await solver_state_service.load(session, run.id)
            hypothesis_item = None
            if isinstance(action, SkillAction):
                handled = await self._handle_skill_action(session, run, challenge, action)
                if not handled:
                    await solver_state_service.record_control_rejection(
                        session, run.id, {"code": "SKILL_NOT_FOUND", "skill_id": action.skill_id, "skill_name": action.skill_name}
                    )
                    current_state = await solver_state_service.load(session, run.id)
                    no_progress_count = current_state.no_progress_count if current_state else 0
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
                    await solver_state_service.record_control_rejection(session, run.id, {"code": "SKILL_DECISION_REQUIRED", "tool": action.tool_name})
                    current_state = await solver_state_service.load(session, run.id)
                    no_progress_count = current_state.no_progress_count if current_state else 0
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
                    await solver_state_service.record_control_rejection(session, run.id, {"tool": action.tool_name, "code": "TOOL_NOT_AVAILABLE"})
                    current_state = await solver_state_service.load(session, run.id)
                    no_progress_count = current_state.no_progress_count if current_state else 0
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
                if fingerprint_state and not action.retry_reason and action.tool_name != "file_read":
                    await event_service.append(
                        session,
                        run.id,
                        "agent.action_rejected",
                        {"tool": action.tool_name, "code": "DUPLICATE_ACTION"},
                    )
                    await solver_state_service.record_control_rejection(session, run.id, {"tool": action.tool_name, "code": "DUPLICATE_ACTION", "fingerprint": fingerprint})
                    await solver_state_service.record_progress(session, run.id, False)
                    current_state = await solver_state_service.load(session, run.id)
                    no_progress_count = current_state.no_progress_count if current_state else 0
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
                    if current_state and current_state.duplicate_action_streak >= 2:
                        recommendation = recovery_planner.plan(phase=run.current_phase, no_progress=no_progress_count, duplicate_streak=current_state.duplicate_action_streak)
                        recommendation["recommended_tool"] = automation_policy_engine.recovery_tool("sql_injection" if "sql" in str(action.hypothesis).lower() else "", action.tool_name)
                        await event_service.append(session, run.id, "agent.automation_required", recommendation)
                        await solver_state_service.require_plan_action(session, run.id, False)
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
                if run.run_total_logical_tool_calls >= run.max_tool_calls:
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
                run.run_total_logical_tool_calls += 1
                run.attempt_logical_tool_calls += 1
                await session.commit()
                try:
                    result = await tool_gateway.invoke(
                        session, run, challenge, action.tool_name, action.arguments
                    )
                    if isinstance(result, dict) and result.get("status") == "COMPLETED":
                        clear_failure(run)
                    await session.commit()
                except DomainError as error:
                    await event_service.append(
                        session,
                        run.id,
                        "agent.action_rejected",
                        {"tool": action.tool_name, "code": error.code, "error": error.message, "details": error.details, "retryable": error.code in {"CODEX_DIRECT_TOOL_FORBIDDEN", "TOOL_INVALID_ARGUMENT", "FILE_NOT_FOUND", "SCRIPT_NOT_SYNCED", "SKILL_NOT_FOUND", "RUN_TOOL_NOT_ALLOWED", "TOOL_NOT_INSTALLED"}},
                    )
                    classification = classify_rejection(error.code)
                    if classification == "CONTROL_REJECTION":
                        run.tool_call_count = max(0, run.tool_call_count - 1)
                        run.run_total_logical_tool_calls = max(0, run.run_total_logical_tool_calls - 1)
                        run.attempt_logical_tool_calls = max(0, run.attempt_logical_tool_calls - 1)
                        await session.commit()
                    rejection = {
                        "tool": action.tool_name,
                        "fingerprint": fingerprint,
                        "reason": error.message,
                        "code": error.code,
                        "classification": classification,
                    }
                    if classification == "CONTROL_REJECTION":
                        await solver_state_service.record_control_rejection(session, run.id, rejection)
                    elif infrastructure_error(error.code, error.stage):
                        record_failure(run, code=error.code, message=error.message, stage=error.stage)
                        await session.commit()
                    else:
                        await solver_state_service.record_rejected_path(session, run.id, rejection)
                        await solver_state_service.record_progress(session, run.id, False)
                    current_state = await solver_state_service.load(session, run.id)
                    no_progress_count = current_state.no_progress_count if current_state else 0
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
                    if infrastructure_error(error.code, error.stage) and int(run.infrastructure_error_streak or 0) >= 2:
                        await session.commit()
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
                infra_result = infrastructure_error(result.get("error_code"), result.get("stage"))
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
                if call and observation and artifact and not infra_result:
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
                    evidence_view = evidence_pipeline.normalize(
                        action.tool_name,
                        result,
                        [artifact.id] if artifact else [],
                    )
                    for capability in evidence_pipeline.infer_capabilities(evidence_view):
                        await solver_state_service.record_capability(
                            session,
                            run.id,
                            capability,
                            evidence={"tool": action.tool_name, "artifact_id": artifact.id if artifact else None},
                        )
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
                    if infra_result:
                        state = await solver_state_service.load(session, run.id)
                        progress["no_progress_count"] = state.no_progress_count if state else 0
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
                if automation_context is not None:
                    await event_service.append(
                        session, run.id, "automation.completed",
                        {
                            "experiment_id": automation_context.experiment_id,
                            "tool": action.tool_name,
                            "status": result.get("status"),
                            "artifact_id": artifact.id if artifact else None,
                            "expected_artifacts": automation_context.expected_artifacts,
                        },
                    )
                if progress["no_progress_count"] >= 2 and not infra_result:
                    await event_service.append(
                        session,
                        run.id,
                        "agent.replan_required",
                        {"reason": "Repeated no-progress actions"},
                    )
                if not infra_result and await self._stop_if_no_progress(session, run, challenge, progress["no_progress_count"]):
                    return
                if infra_result and int(run.infrastructure_error_streak or 0) >= 2:
                    await session.commit()
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
                    elif not infrastructure_error(result.get("error_code"), result.get("stage")) and consecutive_runner_failures >= 2:
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
            finished = await self._finish(session, run, challenge, action, attempt, lease)
            if finished:
                return
            continue
        if RunStatus(run.status) not in TERMINAL:
            await self._transition(session, run, RunStatus.PAUSED_CHECKPOINT)
            await event_service.append(
                session,
                run.id,
                "run.budget_checkpoint",
                {
                    "run_total_agent_steps": run.run_total_agent_steps,
                    "run_total_logical_tool_calls": run.run_total_logical_tool_calls,
                    "requires_user_input": True,
                    "message": "Run 累计预算已到 checkpoint，需继续规划或由用户确认后再结束。",
                },
            )

    async def _finish(
        self, session, run: SolveRun, challenge: Challenge, action: FinishAction,
        attempt=None, lease=None,
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
        if False and action.result == "unsolved" and run.engine_type == "openai_compatible":
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
                await solver_state_service.record_finish_rejection(session, run.id, missing)
                await self._transition(session, run, RunStatus.PLANNING)
                return False
        solved = False
        if action.flag_candidate:
            await self._transition(session, run, RunStatus.VERIFYING_FLAG)
            solved = await flag_service.verify(session, run, challenge, action.flag_candidate)
            if solved:
                # Verification is terminal.  The stop controller has already
                # closed the attempt and invalidated the thread.
                await report_service.generate(session, run, challenge, "solved")
                await event_service.append(session, run.id, "agent.action_completed", {"type": "finish", "result": "solved"})
                with contextlib.suppress(Exception):
                    await runner_client.clear_sessions(run.id)
                return True
        detailed_gate = await finish_gate.evaluate_detailed(
            session, run, challenge, candidate_verified=solved, result=action.result
        )
        allowed, code, message = bool(detailed_gate["allowed"]), str(detailed_gate["code"]), str(detailed_gate["message"])
        if not allowed:
            await event_service.append(
                session,
                run.id,
                "agent.action_rejected",
                {"type": "finish", "code": code, "message": message, "missing_requirements": detailed_gate.get("missing_requirements", [])},
            )
            await solver_state_service.record_finish_rejection(session, run.id, detailed_gate.get("missing_requirements", []))
            await self._transition(session, run, RunStatus.WAITING_CONFIGURATION if code == "WAITING_CONFIGURATION" else RunStatus.WAITING_USER if code == "WAITING_USER" else RunStatus.PLANNING)
            return False
        result = "solved" if action.result == "solved" and solved else "unsolved"
        await self._transition(
            session,
            run,
            RunStatus.COMPLETED_SOLVED if result == "solved" else RunStatus.COMPLETED_UNSOLVED,
        )
        await run_attempt_service.finish(session, run, attempt, lease)
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
            if run.status in {RunStatus.WAITING_USER, RunStatus.PAUSED_CHECKPOINT, RunStatus.PAUSED_RECOVERY, RunStatus.PAUSED_DEPLOYMENT, RunStatus.WAITING_CONFIGURATION, RunStatus.PAUSED_RATE_LIMIT}:
                await self._transition(session, run, RunStatus.PLANNING)
            iterator = engine.continue_run(run.id, user_message)
        else:
            if run.status in {RunStatus.WAITING_USER, RunStatus.PAUSED_CHECKPOINT, RunStatus.PAUSED_RECOVERY, RunStatus.PAUSED_DEPLOYMENT, RunStatus.WAITING_CONFIGURATION, RunStatus.PAUSED_RATE_LIMIT}:
                await self._transition(session, run, RunStatus.PLANNING)
            iterator = engine.resume(run.id)
        auto_turns = 0
        max_auto_turns = max(1, run.max_agent_steps)
        auto_started = monotonic()
        no_progress_turns = 0
        zero_evidence_turns = 0
        # The Codex SDK streams tool events directly and can emit many tool
        # calls inside one iterator turn.  The OpenAI-compatible action loop
        # checks max_tool_calls before invoking a tool, but this event loop
        # previously only checked max_agent_steps after the whole stream,
        # allowing an SDK run to grow without bound.
        existing_events = list(
            (
                await session.scalars(
                    select(RunEvent).where(
                        RunEvent.run_id == run.id,
                        RunEvent.event_type.in_(("tool.requested", "tool.started", "tool.completed", "tool.failed")),
                    )
                )
            ).all()
        )
        seen_tool_refs = {
            tool_ref
            for event in existing_events
            if (tool_ref := logical_tool_budget_ref(event.payload_json or {}))
        }
        max_tool_calls = max(1, int(run.max_tool_calls or 1))
        existing_tool_count = max(
            len(seen_tool_refs), int(run.run_total_logical_tool_calls or 0)
        )
        if existing_tool_count >= max_tool_calls:
            run.last_error_code = "MAX_TOOL_CALLS"
            run.last_error_message = (
                f"Codex tool-call limit reached ({max_tool_calls}); execution paused."
            )
            await self._transition(session, run, RunStatus.PAUSED_CHECKPOINT)
            await event_service.append(
                session,
                run.id,
                "run.budget_exhausted",
                {
                    "reason": "MAX_TOOL_CALLS",
                    "max_tool_calls": max_tool_calls,
                    "run_total_logical_tool_calls": existing_tool_count,
                },
            )
            return
        recovery_nudge_count = 0
        while True:
            await run_attempt_service.heartbeat(session, attempt, lease)
            auto_turns += 1
            run.active_turn_id = str(uuid.uuid4())
            await session.commit()
            before_progress = await self._codex_progress_snapshot(session, run.id)
            turn_tool_refs: set[str] = set()
            recovery_intercepted = False
            role_snapshot = run.role_snapshot_json or {}
            role_limits = role_snapshot.get("limits") if isinstance(role_snapshot.get("limits"), dict) else role_snapshot
            max_tools_per_turn = int(
                role_limits.get("max_tools_per_turn")
                or role_snapshot.get("max_tools_per_turn")
                or run_budget_guard.MAX_TOOLS_PER_TURN
            )
            async for item in iterator:
                if item.event_type in {"tool.requested", "tool.started", "tool.completed", "tool.failed"}:
                    tool_ref = logical_tool_budget_ref(item.payload)
                    if tool_ref:
                        if tool_ref not in seen_tool_refs:
                            if len(turn_tool_refs) >= max_tools_per_turn:
                                run.last_error_code = "TURN_TOOL_BUDGET_EXHAUSTED"
                                run.last_error_message = (
                                    f"Turn tool-call limit reached ({max_tools_per_turn}); execution paused."
                                )
                                with contextlib.suppress(Exception):
                                    await engine.cancel(run.id)
                                await self._transition(session, run, RunStatus.PAUSED_BUDGET)
                                if attempt is not None:
                                    attempt.status = "PAUSED_BUDGET"
                                    attempt.finished_at = datetime.now(UTC)
                                    attempt.error_code = "TURN_TOOL_BUDGET_EXHAUSTED"
                                if lease is not None:
                                    current_lease = await session.get(RunExecutionLease, lease.id)
                                    if current_lease is not None:
                                        await session.delete(current_lease)
                                await session.commit()
                                await event_service.append(
                                    session,
                                    run.id,
                                    "run.budget_exhausted",
                                    {
                                        "reason": "TURN_TOOL_BUDGET_EXHAUSTED",
                                        "max_tools_per_turn": max_tools_per_turn,
                                    },
                                )
                                return
                            turn_tool_refs.add(tool_ref)
                            current_tool_count = max(
                                len(seen_tool_refs),
                                int(run.run_total_logical_tool_calls or 0),
                            )
                            if current_tool_count >= max_tool_calls:
                                run.last_error_code = "MAX_TOOL_CALLS"
                                run.last_error_message = (
                                    f"Codex tool-call limit reached ({max_tool_calls}); execution paused."
                                )
                                with contextlib.suppress(Exception):
                                    await engine.cancel(run.id)
                                await self._transition(session, run, RunStatus.PAUSED_CHECKPOINT)
                                await event_service.append(
                                    session,
                                    run.id,
                                    "run.budget_exhausted",
                                    {
                                        "reason": "MAX_TOOL_CALLS",
                                        "max_tool_calls": max_tool_calls,
                                        "run_total_logical_tool_calls": current_tool_count,
                                    },
                                )
                                return
                            seen_tool_refs.add(tool_ref)
                thread_id = item.payload.get("thread_id")
                if isinstance(thread_id, str):
                    run.codex_thread_id = thread_id
                    await session.commit()
                if item.event_type == "run.failed":
                    event_code = str(item.payload.get("code") or "ENGINE_ERROR")[:100]
                    run.last_error_code = event_code
                    run.last_error_message = str(item.payload.get("message") or "Engine failed")[:4000]
                    if event_code in {"MCP_PROCESS_START_FAILED", "MCP_INITIALIZE_FAILED", "MCP_TOOL_CATALOG_FAILED", "MCP_SCHEMA_INVALID", "MCP_BACKEND_UNREACHABLE", "DATABASE_MIGRATION_MISMATCH", "RUNNER_CONFIGURATION_INVALID", "CODEX_CLI_EXITED", "CODEX_THREAD_CREATE_FAILED"}:
                        if RunStatus(run.status) != RunStatus.WAITING_CONFIGURATION:
                            await self._transition(session, run, RunStatus.WAITING_CONFIGURATION)
                        await session.commit()
                        await event_service.append(session, run.id, "run.configuration_blocked", {"code": event_code, "diagnostic_artifact": item.payload.get("diagnostic_artifact"), "stage": item.payload.get("stage")})
                        continue
                    if event_code == "CODEX_STREAM_INTERRUPTED":
                        await self._transition(session, run, RunStatus.PAUSED_RECOVERY)
                        await session.commit()
                        await event_service.append(session, run.id, "run.paused_recovery", {"code": event_code})
                        return
                    await session.commit()
                if item.status and item.status != run.status:
                    incoming_status = RunStatus(item.status)
                    # A recreated/resumed Codex thread reports its bootstrap
                    # state as ANALYZING even after the orchestrator has
                    # already moved the durable run to PLANNING.  The engine
                    # adapters filter this in the normal path; keep the
                    # orchestrator defensive because old Bridge processes or
                    # concurrent resumes can still forward that stale event.
                    actionable_checkpoint = (
                        isinstance(run.recovery_checkpoint_json, dict)
                        and run.recovery_checkpoint_json.get("next_required_action")
                        and run.current_phase == "FLAG_SEARCH"
                    )
                    if incoming_status == RunStatus.WAITING_USER and actionable_checkpoint:
                        # A provider clarification turn is not a valid reason to
                        # stop a controller-owned recovery checkpoint. Preserve
                        # the provider event for audit, but immediately nudge the
                        # live Codex thread with the required bounded action.
                        recovery_intercepted = True
                    elif not (
                        RunStatus(run.status) == RunStatus.PLANNING
                        and incoming_status == RunStatus.ANALYZING
                    ):
                        await self._transition(session, run, incoming_status)
                await event_service.append(session, run.id, item.event_type, item.payload)
                if item.event_type == "agent.message":
                    # Codex SDK turns do not expose the legacy structured
                    # FinishAction channel.  When the model reports a
                    # displayable flag after the bounded evidence already
                    # exists, complete the same verification gate used by the
                    # structured engine and perform fresh reproduction.
                    message = str(item.payload.get("message") or "")
                    match = re.search(r"(?i)flag\{[^{}\r\n\"\\]{1,256}\}", message)
                    if match and RunStatus(run.status) not in TERMINAL:
                        try:
                            challenge = await session.get(Challenge, run.challenge_id)
                            steps = await ReproductionPlanner().plan(session, run, challenge)
                            validation = await fresh_reproduction_executor.execute(session, run, challenge, steps)
                            if validation.get("verified"):
                                finished = await self._finish(
                                    session,
                                    run,
                                    challenge,
                                    FinishAction(
                                        type="finish",
                                        result="solved",
                                        summary="Codex SDK reported a dynamically extracted candidate and fresh reproduction verified it.",
                                        flag_candidate=match.group(0),
                                    ),
                                    attempt,
                                    lease,
                                )
                                if finished:
                                    return
                        except Exception:
                            # A normal SDK message must never become an engine
                            # error solely because finalization needs another
                            # bounded attempt; the next turn can retry it.
                            pass
                if item.event_type == "tool.completed":
                    rate_limit_status = _tool_rate_limit_status(item.payload)
                    if rate_limit_status is not None:
                        run.last_error_code = "TARGET_RATE_LIMITED"
                        run.last_error_message = (
                            "Target returned HTTP 429; execution paused to avoid repeated requests."
                        )
                        with contextlib.suppress(Exception):
                            await engine.cancel(run.id)
                        await self._transition(session, run, RunStatus.PAUSED_RATE_LIMIT)
                        await session.commit()
                        await event_service.append(
                            session,
                            run.id,
                            "run.paused_rate_limit",
                            {
                                "code": "TARGET_RATE_LIMITED",
                                "status_code": rate_limit_status,
                                "message": run.last_error_message,
                            },
                        )
                        return
            run.active_turn_id = None
            await session.commit()
            await codex_materializer.sync(session, run)
            interval = max(1, int(run.agent_checkpoint_interval or 30))
            if run.agent_step_count and run.agent_step_count % interval == 0:
                await self._transition(session, run, RunStatus.PAUSED_CHECKPOINT)
                await event_service.append(session, run.id, "run.checkpoint_reached", {"step": run.agent_step_count, "interval": interval, "phase": run.current_phase, "remaining_steps": max(0, run.max_agent_steps - run.agent_step_count)})
                return
            after_progress = await self._codex_progress_snapshot(session, run.id)
            if after_progress[:3] == before_progress[:3]:
                zero_evidence_turns += 1
                await event_service.append(
                    session,
                    run.id,
                    "codex.runtime_diagnostic",
                    {
                        "code": "CODEX_RUNTIME_DIAGNOSTIC",
                        "turn": auto_turns,
                        "tool_calls": 0,
                        "artifacts": 0,
                        "observations": 0,
                        "action": "REPLAN_RETRY",
                    },
                )
                no_progress_turns = 0
                if zero_evidence_turns >= 3:
                    # A live provider that repeatedly emits no actionable
                    # work is a method stall, not missing configuration.
                    run.last_error_code = "METHOD_STALLED"
                    run.last_error_message = "Codex produced no ToolCall, Artifact, or Observation after repeated replans."
                    await self._transition(session, run, RunStatus.PAUSED_CHECKPOINT)
                    await event_service.append(
                        session,
                        run.id,
                        "run.recovery_checkpoint",
                        {"code": "METHOD_STALLED", "required_action": "REPLAN_WITH_NEW_EVIDENCE", "diagnostic": "CODEX_RUNTIME_DIAGNOSTIC"},
                    )
                    return
            else:
                zero_evidence_turns = 0
            if after_progress == before_progress:
                no_progress_turns += 1
            else:
                no_progress_turns = 0
            current_status = RunStatus(run.status)
            if recovery_intercepted and recovery_nudge_count < 3:
                recovery_nudge_count += 1
                if current_status != RunStatus.PLANNING:
                    await self._transition(session, run, RunStatus.PLANNING)
                await event_service.append(
                    session,
                    run.id,
                    "agent.recovery_nudge",
                    {
                        "code": "WAITING_USER_INTERCEPTED",
                        "nudge": recovery_nudge_count,
                        "required_action": "BOUNDED_EXTRACTION",
                    },
                )
                iterator = engine.continue_run(
                    run.id,
                    "Controller directive: continue autonomously now. Execute the checkpoint's bounded extraction; do not ask for user input or model configuration.",
                )
                continue
            if current_status in TERMINAL or current_status in {RunStatus.WAITING_USER, RunStatus.WAITING_CONFIGURATION}:
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
            if no_progress_turns == 2:
                run.recovery_checkpoint_json = {**(run.recovery_checkpoint_json or {}), "force_script_fallback": True}
                await session.commit()
                challenge = await session.get(Challenge, run.challenge_id)
                attempt_row = await session.scalar(select(RunAttempt).where(RunAttempt.run_id == run.id, RunAttempt.status == "RUNNING"))
                lease_row = await session.scalar(select(RunExecutionLease).where(RunExecutionLease.run_id == run.id))
                if challenge and attempt_row and lease_row:
                    await script_fallback_controller.run(session, run, challenge, attempt_row, lease_row)
            if no_progress_turns >= 3:
                run.last_error_code = "METHOD_STALLED"
                run.last_error_message = "Codex produced three turns without durable ToolCall, Artifact, Observation, or ScriptRecord progress."
                await self._transition(session, run, RunStatus.PAUSED_CHECKPOINT)
                await session.commit()
                await event_service.append(session, run.id, "run.recovery_checkpoint", {"code": "METHOD_STALLED", "required_action": "BOUNDED_SCRIPT_EXTRACTION"})
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
        task = self.active_tasks.get(run_id)
        try:
            if isinstance(engine, SolveEngine):
                await engine.cancel(run_id)
        except Exception:
            # Bridge cancellation is optional. The local orchestrator task
            # must still be cancelled when the Bridge returns 501 or fails.
            pass
        finally:
            if task and task is not asyncio.current_task():
                task.cancel()


orchestrator = SolveOrchestrator()
