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
    # The service intentionally does not resume autonomous work after a restart.
    async with SessionLocal() as session:
        await builtin_skill_sync_service.sync(session)
        await run_attempt_service.reconcile_startup(session)
        runs = list((await session.scalars(select(SolveRun))).all())
        for run in runs:
            if RunStatus(run.status) not in TERMINAL and run.status != RunStatus.CREATED:
                transition(run, RunStatus.FAILED_ENGINE)
                run.last_error_code, run.last_error_message = (
                    "INTERRUPTED_RESTART",
                    "Run was interrupted by a service restart.",
                )
                await session.commit()
                await event_service.append(
                    session, run.id, "run.failed", {"code": "INTERRUPTED_RESTART"}
                )
    yield


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
