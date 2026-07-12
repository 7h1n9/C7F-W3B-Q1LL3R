import asyncio
from pathlib import Path
from time import monotonic

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.exceptions import DomainError
from app.engines import (
    CodexSdkEngine,
    MockSolveEngine,
    ModelRateLimitError,
    ModelUnavailableError,
    OpenAICompatibleEngine,
    SolveEngine,
)
from app.models.challenge import Challenge
from app.models.model_config import ModelConfig
from app.models.run import Artifact, Observation, SolveRun, ToolCall
from app.orchestration.state_machine import TERMINAL, RunStatus, transition
from app.schemas.agent import FinishAction, ToolAction
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
from app.services.runner_client import runner_client
from app.services.solver_state import solver_state_service
from app.tools.gateway import tool_gateway
from app.tools.registry import load_tool_definitions


class SolveOrchestrator:
    def __init__(self, engine_factory=None) -> None:
        self.engine_factory = engine_factory
        self.active_engines: dict[str, object] = {}
        self.active_tasks: dict[str, asyncio.Task[None]] = {}

    async def build_engine(self, run: SolveRun, session) -> object:
        if self.engine_factory:
            return self.engine_factory(run)
        if run.engine_type == "codex_sdk":
            return CodexSdkEngine(get_settings().codex_bridge_url, run.workspace_path)
        if run.engine_type == "openai_compatible":
            config = (
                await session.get(ModelConfig, run.model_config_id) if run.model_config_id else None
            )
            if not config or not config.enabled or not config.base_url or not config.model_name:
                raise ValueError("OpenAI-compatible engine requires an enabled model configuration")
            return OpenAICompatibleEngine(
                config.base_url, decrypt_api_key(config.encrypted_api_key), config.model_name
            )
        return MockSolveEngine()

    async def _transition(self, session, run: SolveRun, target: RunStatus) -> None:
        if RunStatus(run.status) == target:
            return
        transition(run, target)
        await session.commit()
        await solver_state_service.sync_from_run(session, run)
        await event_service.append(session, run.id, "run.status_changed", {"status": run.status})

    async def start(self, run_id: str, user_message: str | None = None) -> None:
        task = asyncio.current_task()
        if task:
            self.active_tasks[run_id] = task
        async with SessionLocal() as session:
            try:
                run = await session.scalar(select(SolveRun).where(SolveRun.id == run_id))
                if not run:
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
                engine = await self.build_engine(run, session)
                self.active_engines[run_id] = engine
                if run.engine_type == "openai_compatible":
                    await self._run_openai(session, run, engine, user_message)
                else:
                    await self._run_event_engine(session, run, engine, user_message)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if "run" in locals() and RunStatus(run.status) not in TERMINAL:
                    if isinstance(error, ModelRateLimitError):
                        code = "MODEL_RATE_LIMITED"
                    elif isinstance(error, ModelUnavailableError):
                        code = "MODEL_UNAVAILABLE"
                    else:
                        code = "ENGINE_ERROR"
                    run.last_error_code, run.last_error_message = code, str(error)[:4000]
                    await self._transition(session, run, RunStatus.FAILED_ENGINE)
                    await event_service.append(
                        session,
                        run_id,
                        "run.failed",
                        {"code": code, "message": str(error)[:1000]},
                    )
            finally:
                self.active_engines.pop(run_id, None)
                self.active_tasks.pop(run_id, None)

    async def _run_openai(
        self, session, run: SolveRun, engine: OpenAICompatibleEngine, user_message: str | None
    ) -> None:
        if run.status == RunStatus.CREATED:
            await self._transition(session, run, RunStatus.PREPARING)
            await event_service.append(session, run.id, "run.started", {})
            await self._transition(session, run, RunStatus.ANALYZING)
        elif run.status == RunStatus.WAITING_USER:
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
            if RunStatus(run.status) == RunStatus.CANCELLED:
                return
            if monotonic() - started > run.max_runtime_seconds:
                await self._transition(session, run, RunStatus.TIMEOUT)
                return
            if RunStatus(run.status) in {RunStatus.ANALYZING, RunStatus.EVALUATING}:
                await self._transition(session, run, RunStatus.PLANNING)
            messages = await context_builder.build(session, run, challenge)
            if user_message:
                messages.append({"role": "user", "content": f"User supplied: {user_message}"})
                user_message = None
            action = await engine.next_action(messages)
            run.agent_step_count += 1
            await session.commit()
            await event_service.append(
                session,
                run.id,
                "agent.action_requested",
                {
                    "type": action.type,
                    "phase": getattr(action, "phase", None),
                    "objective": getattr(action, "objective", None),
                    "hypothesis": getattr(action, "hypothesis", None),
                    "reason": action.reason if isinstance(action, ToolAction) else action.summary,
                    "expected_evidence": getattr(action, "expected_evidence", None),
                    "success_condition": getattr(action, "success_condition", None),
                    "failure_pivot": getattr(action, "failure_pivot", None),
                    "retry_reason": getattr(action, "retry_reason", None),
                    "activate_skill": getattr(action, "activate_skill", None),
                },
            )
            hypothesis_item = None
            if isinstance(action, ToolAction):
                hypothesis_item, created = await hypothesis_service.upsert_from_action(
                    session,
                    run.id,
                    phase=getattr(action, "phase", None),
                    objective=getattr(action, "objective", None),
                    hypothesis_text=getattr(action, "hypothesis", None),
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
                        {"tool": action.tool_name, "code": error.code},
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
                progress = {"made_progress": False, "no_progress_count": 0, "activated_skill_ids": []}
                if call and observation and artifact:
                    progress = await progress_evaluator.evaluate(
                        session,
                        run,
                        challenge,
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
                    for skill_id in progress["activated_skill_ids"]:
                        await event_service.append(
                            session,
                            run.id,
                            "skill.activated",
                            {"skill_id": skill_id, "source": "observation"},
                        )
                    await event_service.append(
                        session,
                        run.id,
                        "agent.progress_detected",
                        {
                            "tool": action.tool_name,
                            "no_progress_count": progress["no_progress_count"],
                            "activated_skill_ids": progress["activated_skill_ids"],
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
                    if consecutive_runner_failures >= 2:
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
        return True

    async def _run_event_engine(
        self, session, run: SolveRun, engine: SolveEngine, user_message: str | None
    ) -> None:
        if run.status == RunStatus.CREATED:
            await self._transition(session, run, RunStatus.PREPARING)
            await event_service.append(session, run.id, "run.started", {})
            iterator = engine.start(run.id)
        elif user_message:
            iterator = engine.continue_run(run.id, user_message)
        else:
            iterator = engine.resume(run.id)
        async for item in iterator:
            thread_id = item.payload.get("thread_id")
            if isinstance(thread_id, str):
                run.codex_thread_id = thread_id
                await session.commit()
            if item.status and item.status != run.status:
                await self._transition(session, run, RunStatus(item.status))
            await event_service.append(session, run.id, item.event_type, item.payload)
        await codex_materializer.sync(session, run)

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
