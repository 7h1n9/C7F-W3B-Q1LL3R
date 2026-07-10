import asyncio

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.engines import CodexSdkEngine, MockSolveEngine, OpenAICompatibleEngine, SolveEngine
from app.models.model_config import ModelConfig
from app.models.run import SolveRun
from app.orchestration.state_machine import TERMINAL, RunStatus, transition
from app.services.events import event_service


class SolveOrchestrator:
    def __init__(self, engine_factory=None) -> None:
        self.engine_factory = engine_factory
        self.active_engines: dict[str, SolveEngine] = {}
        self.active_tasks: dict[str, asyncio.Task[None]] = {}

    async def build_engine(self, run: SolveRun, session) -> SolveEngine:
        if self.engine_factory:
            return self.engine_factory(run)
        if run.engine_type == "codex_sdk":
            return CodexSdkEngine(get_settings().codex_bridge_url, run.workspace_path)
        if run.engine_type == "openai_compatible":
            config = await session.get(ModelConfig, run.model_config_id) if run.model_config_id else None
            if not config or not config.base_url or not config.model_name or not config.encrypted_api_key:
                raise ValueError("OpenAI-compatible engine requires an enabled model configuration")
            return OpenAICompatibleEngine(config.base_url, config.encrypted_api_key, config.model_name)
        return MockSolveEngine()

    async def start(self, run_id: str) -> None:
        task = asyncio.current_task()
        if task:
            self.active_tasks[run_id] = task
        async with SessionLocal() as session:
            try:
                run = await session.scalar(select(SolveRun).where(SolveRun.id == run_id))
                if not run:
                    return
                transition(run, RunStatus.PREPARING)
                await session.commit()
                await event_service.append(session, run_id, "run.status_changed", {"status": run.status})
                await event_service.append(session, run_id, "run.started", {})
                engine = await self.build_engine(run, session)
                self.active_engines[run_id] = engine
                async for item in engine.start(run_id):
                    thread_id = item.payload.get("thread_id")
                    if isinstance(thread_id, str):
                        run.codex_thread_id = thread_id
                        await session.commit()
                    if item.status and item.status != run.status:
                        transition(run, RunStatus(item.status))
                        await session.commit()
                        await event_service.append(session, run_id, "run.status_changed", {"status": run.status})
                    await event_service.append(session, run_id, item.event_type, item.payload)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if "run" in locals() and RunStatus(run.status) not in TERMINAL:
                    transition(run, RunStatus.FAILED_ENGINE)
                    await session.commit()
                    await event_service.append(session, run_id, "run.failed", {"code": "ENGINE_ERROR", "message": str(error)})
                    await event_service.append(session, run_id, "run.status_changed", {"status": run.status})
            finally:
                self.active_engines.pop(run_id, None)
                self.active_tasks.pop(run_id, None)

    async def cancel(self, run_id: str) -> None:
        engine = self.active_engines.get(run_id)
        if engine:
            await engine.cancel(run_id)
        task = self.active_tasks.get(run_id)
        if task and task is not asyncio.current_task():
            task.cancel()


orchestrator = SolveOrchestrator()
