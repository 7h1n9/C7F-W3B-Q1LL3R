import asyncio
from time import monotonic

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.exceptions import DomainError
from app.engines import CodexSdkEngine, MockSolveEngine, OpenAICompatibleEngine, SolveEngine
from app.models.challenge import Challenge
from app.models.model_config import ModelConfig
from app.models.run import SolveRun
from app.orchestration.state_machine import TERMINAL, RunStatus, transition
from app.schemas.agent import FinishAction, ToolAction
from app.services.context_builder import context_builder
from app.services.crypto import decrypt_api_key
from app.services.events import event_service
from app.services.flags import flag_service
from app.services.reports import report_service
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
            config = await session.get(ModelConfig, run.model_config_id) if run.model_config_id else None
            if not config or not config.enabled or not config.base_url or not config.model_name:
                raise ValueError("OpenAI-compatible engine requires an enabled model configuration")
            return OpenAICompatibleEngine(config.base_url, decrypt_api_key(config.encrypted_api_key), config.model_name)
        return MockSolveEngine()

    async def _transition(self, session, run: SolveRun, target: RunStatus) -> None:
        if RunStatus(run.status) == target:
            return
        transition(run, target)
        await session.commit()
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
                    run.last_error_code, run.last_error_message = "ENGINE_ERROR", str(error)[:4000]
                    await self._transition(session, run, RunStatus.FAILED_ENGINE)
                    await event_service.append(session, run_id, "run.failed", {"code": "ENGINE_ERROR", "message": str(error)[:1000]})
            finally:
                self.active_engines.pop(run_id, None)
                self.active_tasks.pop(run_id, None)

    async def _run_openai(self, session, run: SolveRun, engine: OpenAICompatibleEngine, user_message: str | None) -> None:
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
            await event_service.append(session, run.id, "agent.action_requested", {"type": action.type, "reason": action.reason if isinstance(action, ToolAction) else action.summary})
            if isinstance(action, ToolAction):
                if action.tool_name not in load_tool_definitions():
                    await event_service.append(session, run.id, "agent.action_rejected", {"tool": action.tool_name, "code": "TOOL_NOT_AVAILABLE"})
                    continue
                if run.tool_call_count >= run.max_tool_calls:
                    await event_service.append(session, run.id, "agent.action_rejected", {"tool": action.tool_name, "code": "MAX_TOOL_CALLS"})
                    break
                await self._transition(session, run, RunStatus.EXECUTING)
                run.tool_call_count += 1
                await session.commit()
                try:
                    result = await tool_gateway.invoke(session, run, challenge, action.tool_name, action.arguments)
                except DomainError as error:
                    await event_service.append(session, run.id, "agent.action_rejected", {"tool": action.tool_name, "code": error.code})
                    await self._transition(session, run, RunStatus.EVALUATING)
                    continue
                await event_service.append(session, run.id, "agent.action_completed", {"type": "tool", "tool": action.tool_name, "status": result.get("status")})
                await self._transition(session, run, RunStatus.EVALUATING)
                continue
            await self._finish(session, run, challenge, action)
            return
        if RunStatus(run.status) not in TERMINAL:
            await self._transition(session, run, RunStatus.REPORTING)
            await report_service.generate(session, run, challenge, "unsolved", "Maximum agent steps or tool calls reached")
            await self._transition(session, run, RunStatus.COMPLETED_UNSOLVED)

    async def _finish(self, session, run: SolveRun, challenge: Challenge, action: FinishAction) -> None:
        if action.result == "waiting_user":
            await self._transition(session, run, RunStatus.WAITING_USER)
            await event_service.append(session, run.id, "agent.action_completed", {"type": "finish", "result": "waiting_user"})
            return
        solved = False
        if action.flag_candidate:
            await self._transition(session, run, RunStatus.VERIFYING_FLAG)
            solved = await flag_service.verify(session, run, challenge, action.flag_candidate)
        await self._transition(session, run, RunStatus.REPORTING)
        result = "solved" if action.result == "solved" and solved else "unsolved"
        await report_service.generate(session, run, challenge, result, "Flag did not match the configured pattern" if action.result == "solved" and not solved else "")
        await event_service.append(session, run.id, "agent.action_completed", {"type": "finish", "result": result})
        await self._transition(session, run, RunStatus.COMPLETED_SOLVED if result == "solved" else RunStatus.COMPLETED_UNSOLVED)

    async def _run_event_engine(self, session, run: SolveRun, engine: SolveEngine, user_message: str | None) -> None:
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
