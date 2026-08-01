import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
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
from app.services.multi_agent import deterministic_controller
from app.services.run_attempts import run_attempt_service
from app.services.run_finalizer import run_finalizer
from app.services.temporary_data import temporary_data_janitor


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
        await deterministic_controller.seed_policies(session)
        await run_attempt_service.cleanup_tickets(session)
        await run_attempt_service.reconcile_startup(session)
        # Reconcile every persisted Run after process restart.  This also
        # removes leases left by a previously terminated process and repairs
        # phase projections before the first resume request arrives.
        for run in (await session.scalars(select(SolveRun))).all():
            await run_finalizer.reconcile(session, run)
        recovery = await deterministic_controller.reconcile_startup(session)
        for run_id in recovery.get("run_ids", []):
            run = await session.get(SolveRun, run_id)
            if run and RunStatus(run.status) not in TERMINAL:
                run.status = RunStatus.PAUSED_RECOVERY.value
                run.last_error_code = "SERVICE_RESTART_INTERRUPTED_TASK"
                run.last_error_message = "A role task was interrupted by service restart; durable evidence is available for a fresh lease."
                await event_service.append(session, run.id, "run.paused_recovery", {"classification": "SERVICE_RESTART_INTERRUPTED_TASK", "retryable": True})
        await session.commit()
        # Startup recovery is handled above by the durable task/attempt
        # reconciler. Do not blanket-transition every run to deployment pause.
        for run in ():
            if False:
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
            await asyncio.sleep(300)
            async with SessionLocal() as cleanup_session:
                await run_attempt_service.cleanup_tickets(cleanup_session)
                await temporary_data_janitor.run_once(cleanup_session)

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


def _error_payload(code: str, message: str, *, stage: str, status_code: int, details: object = None) -> dict:
    from uuid import uuid4
    return {
        "code": code,
        "message": message,
        "stage": stage,
        "retryable": status_code >= 500,
        "diagnostic_id": str(uuid4()),
        "tool_execution_completed": False,
        "details": _json_safe(details or {}),
    }


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
        content=_error_payload("MCP_VALIDATION_FAILED", "Request validation failed.", stage="VALIDATION", status_code=422, details={"errors": error.errors()}),
    )


@app.exception_handler(HTTPException)
async def http_error(_: Request, error: HTTPException) -> JSONResponse:
    detail = error.detail if isinstance(error.detail, dict) else {"detail": error.detail}
    code = str(detail.get("code") or "HTTP_ERROR") if isinstance(detail, dict) else "HTTP_ERROR"
    message = str(detail.get("message") or detail.get("detail") or "Request failed") if isinstance(detail, dict) else str(detail)
    return JSONResponse(status_code=error.status_code, content=_error_payload(code, message, stage="HTTP", status_code=error.status_code, details=detail))


@app.exception_handler(Exception)
async def unhandled_error(_: Request, error: Exception) -> JSONResponse:
    return JSONResponse(status_code=500, content=_error_payload("BACKEND_UNAVAILABLE", "Backend request failed.", stage="BACKEND", status_code=500, details={"error": str(error)[:1000]}))


app.include_router(router)
