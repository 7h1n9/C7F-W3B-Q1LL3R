import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.api.v1 import router
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.exceptions import DomainError, domain_error_handler
from app.models.run import SolveRun
from app.orchestration.state_machine import TERMINAL, RunStatus, transition
from app.services.builtin_skills import builtin_skill_sync_service
from app.services.events import event_service
from app.services.run_attempts import run_attempt_service


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    settings.require_safe_production_secrets()
    if settings.codex_diagnostics_enabled:
        # Diagnostic mode must preserve the live incident scene: do not close
        # attempts, delete leases, fail in-flight runs, or start recovery work.
        yield
        return
    async with SessionLocal() as session:
        await builtin_skill_sync_service.sync(session)
        await run_attempt_service.cleanup_tickets(session)
        await run_attempt_service.reconcile_startup(session)
        runs = list((await session.scalars(select(SolveRun))).all())
        for run in runs:
            if RunStatus(run.status) not in TERMINAL and run.status not in {
                RunStatus.CREATED,
                RunStatus.PAUSED_RECOVERY,
                RunStatus.PAUSED_DEPLOYMENT,
            }:
                transition(run, RunStatus.PAUSED_DEPLOYMENT)
                run.last_error_code, run.last_error_message = (
                    "PAUSED_DEPLOYMENT",
                    "服务重启后任务已保留为可恢复状态。",
                )
                await session.commit()
                await event_service.append(
                    session, run.id, "run.paused_deployment", {"code": "PAUSED_DEPLOYMENT"}
                )
    async def cleanup_loop() -> None:
        while True:
            await asyncio.sleep(600)
            async with SessionLocal() as cleanup_session:
                await run_attempt_service.cleanup_tickets(cleanup_session)

    cleanup_task = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task


app = FastAPI(title="CTF Web Agent API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_exception_handler(DomainError, domain_error_handler)


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, error: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "code": "VALIDATION_ERROR",
            "message": "Request validation failed.",
            "details": {"errors": _json_safe(error.errors())},
        },
    )


app.include_router(router)
