import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager

# The official Muteki Worker Sandbox uses the reverse-connect control plane:
# containers dial the host receiver through ``host.docker.internal``.  A
# loopback-only listener is not a valid production boundary for that topology
# on Docker Desktop.  Keep the per-run token handshake as the authorization
# boundary, but make the listener container-reachable before any Muteki module
# imports its bind constants.  Operators can still explicitly override it.
os.environ.setdefault("MUTEKI_CONTROL_BIND", "0.0.0.0")
os.environ.setdefault("MUTEKI_CONTROL_PORT", "9100")

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.api.claude_proxy import router as claude_proxy_router
from app.api.codex_proxy import router as codex_proxy_router
from app.api.v1 import router
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.exceptions import DomainError, domain_error_handler
from app.models.run import SolveRun
from app.orchestration.state_machine import TERMINAL, RunStatus, parse_run_status, transition
from app.services.arena_queue import arena_queue_scheduler
from app.services.builtin_skills import builtin_skill_sync_service
from app.services.claude_proxy_supervisor import claude_proxy_supervisor
from app.services.codex_proxy_supervisor import codex_proxy_supervisor
from app.services.events import event_service
from app.services.multi_agent import deterministic_controller
from app.services.run_attempts import run_attempt_service
from app.services.run_finalizer import run_finalizer
from app.services.run_supervisor import run_supervisor
from app.services.runtime_reconciler import reconcile_runtime_state
from app.services.temporary_data import temporary_data_janitor

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    settings.require_safe_production_secrets()
    if settings.codex_diagnostics_enabled:
        # Diagnostic mode must preserve the live incident scene: do not close
        # attempts, delete leases, fail in-flight runs, or start recovery work.
        yield
        return

    # Only the cheap, order-independent setup runs before the listening socket
    # opens.  Everything proportional to the persisted Run history is deferred to
    # ``_reconcile_persisted_runs``; see that function for why.
    async with SessionLocal() as session:
        await builtin_skill_sync_service.sync(session)
        await deterministic_controller.seed_policies(session)
        await run_attempt_service.cleanup_tickets(session)

    async def cleanup_loop() -> None:
        # ``reconcile_runtime_state`` also mutates leases, so it must not race
        # the startup pass that owns the same rows.
        with contextlib.suppress(Exception):
            await startup_reconcile
        while True:
            await asyncio.sleep(10)
            async with SessionLocal() as cleanup_session:
                await reconcile_runtime_state(cleanup_session)
                # The janitor is intentionally less frequent than runtime
                # reconciliation because it scans temporary filesystem data.
                if int(asyncio.get_running_loop().time()) % 30 < 10:
                    await run_attempt_service.cleanup_tickets(cleanup_session)
                if int(asyncio.get_running_loop().time()) % 300 < 10:
                    await temporary_data_janitor.run_once(cleanup_session)

    async def supervisor_loop() -> None:
        # The first tick is the first thing that can resume a Run, so it must wait
        # until stale leases from the previous process have been reconciled away.
        with contextlib.suppress(Exception):
            await startup_reconcile
        while True:
            await asyncio.sleep(5)
            async with SessionLocal() as supervisor_session:
                paused_runs = list((await supervisor_session.scalars(select(SolveRun).where(
                    SolveRun.status.in_([
                        RunStatus.PAUSED_RECOVERY.value,
                        RunStatus.PAUSED_CHECKPOINT.value,
                        RunStatus.WAITING_USER.value,
                        RunStatus.PLANNING.value,
                        RunStatus.ANALYZING.value,
                        RunStatus.EVALUATING.value,
                    ])
                ))).all())
            for paused_run in paused_runs:
                with contextlib.suppress(Exception):
                    # Re-read the status through the tolerant parser: the row was
                    # selected by a query in another session, so between that read
                    # and this one it may carry a value the enum does not know.
                    # Skipping is deliberate -- recovering a Run whose state we
                    # cannot interpret would mean guessing at it, and the row stays
                    # eligible for the next sweep.
                    paused_status = parse_run_status(paused_run.status)
                    if paused_status is None:
                        logger.warning(
                            "Skipping checkpoint recovery for run_id=%s: unreadable status %r",
                            paused_run.id,
                            paused_run.status,
                        )
                        continue
                    if paused_status == RunStatus.WAITING_USER:
                        async with SessionLocal() as recovery_session:
                            current = await recovery_session.get(SolveRun, paused_run.id)
                            if current and await run_supervisor.has_unfinished_user_input(recovery_session, current):
                                await run_supervisor.enqueue(current.id, reason="USER_INPUT_CONSUMED")
                    else:
                        await run_supervisor.enqueue(paused_run.id, reason="CHECKPOINT_RECOVERY")

    await run_supervisor.start_worker()
    startup_reconcile = asyncio.create_task(
        _reconcile_persisted_runs(), name="startup-reconcile"
    )
    cleanup_task = asyncio.create_task(cleanup_loop())
    supervisor_task = asyncio.create_task(supervisor_loop())
    # Return any arena queue item stranded in RUNNING by the previous process.
    # Done eagerly rather than lazily so the listing never shows a phantom
    # in-flight item. The scheduler itself is not auto-started: a batch spends the
    # platform's single environment slot for hours, so starting one is an explicit
    # operator action.
    with contextlib.suppress(Exception):
        await arena_queue_scheduler.recover()
    # Destroy an environment left behind by the previous process.  Backgrounded
    # on purpose: the call is outbound to the arena and startup must stay
    # constant, but the work itself is not optional -- the platform allows one
    # environment per agent, so a leaked one blocks every later solve with an
    # error that points somewhere else entirely.
    orphan_reclaim = asyncio.create_task(
        _reclaim_arena_environment(), name="arena-orphan-reclaim"
    )
    # The protocol-translating proxies are separate processes (each must bind
    # 0.0.0.0 on a random port so Worker containers can reach them by host
    # alias). Started here so a CLI channel works without a second terminal;
    # suppressed on failure because a missing proxy only costs a fallback to the
    # stored base_url. Two of them, one per CLI wire protocol: Claude speaks
    # Anthropic Messages, Codex speaks Responses, and each needs its own
    # translation when the configured relay only offers chat.
    with contextlib.suppress(Exception):
        await claude_proxy_supervisor.start()
    with contextlib.suppress(Exception):
        await codex_proxy_supervisor.start()
    try:
        yield
    finally:
        cleanup_task.cancel()
        supervisor_task.cancel()
        startup_reconcile.cancel()
        orphan_reclaim.cancel()
        with contextlib.suppress(Exception):
            await claude_proxy_supervisor.stop()
        with contextlib.suppress(Exception):
            await codex_proxy_supervisor.stop()
        with contextlib.suppress(Exception):
            await arena_queue_scheduler.stop()
        # A graceful shutdown is still a process that will not be around to
        # destroy its environment.  The breadcrumb would let the next start
        # reclaim it, but doing it here is cheaper and leaves the single slot
        # free for the boot that follows instead of occupied until it runs.
        with contextlib.suppress(Exception):
            from app.services.arena_lease import arena_lease_manager

            await arena_lease_manager.release()
        await run_supervisor.stop_worker()
        for task in (cleanup_task, supervisor_task, startup_reconcile, orphan_reclaim):
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def _reclaim_arena_environment() -> None:
    """Destroy the arena environment a previous process left behind.

    The platform allows **one** environment per agent.  After a crash between
    ``start_environment`` and ``delete_environment`` the slot stays taken with no
    in-process record of it, and every later solve fails with an error that points
    at whatever it tried first -- an outage whose cause is nowhere near its
    symptom.  The lease manager writes a breadcrumb as it takes the slot, so this
    can name the environment without a human guessing, and it is idempotent for
    the ordinary case where there is nothing to reclaim.

    Failures are logged, never raised: the service is already serving traffic, and
    an unreachable arena at boot is a condition an operator can see on the status
    page rather than a reason to refuse to start.
    """

    from app.models.arena import ArenaConnection
    from app.services.arena_lease import reclaim_persisted_lease
    from app.services.arena_mcp import arena_mcp_clients
    from app.services.crypto import decrypt_api_key

    try:
        async with SessionLocal() as session:
            connection = await session.scalar(
                select(ArenaConnection).where(ArenaConnection.enabled.is_(True))
            )
            if connection is None:
                return
            connection_id = connection.id
            token = decrypt_api_key(connection.encrypted_token) if connection.encrypted_token else None
            client = await arena_mcp_clients.get(connection, token)
        result = await reclaim_persisted_lease(client, connection_id=connection_id)
        if result.get("reclaimed"):
            logger.warning(
                "Reclaimed an arena environment left by a previous process: %s",
                result.get("lease"),
            )
    except Exception:  # noqa: BLE001 - startup must proceed regardless
        logger.warning("Arena orphan reclaim failed at startup", exc_info=True)


async def _reconcile_persisted_runs() -> None:
    """Bring durable Run state back in line after a process restart.

    This pass is proportional to the size of the Run history: it loads every
    persisted Run, reconciles each one, and asks Docker about the containers of
    every Muteki Run.  uvicorn 0.51 binds its listening socket only *after*
    ``lifespan`` startup returns, so running this inline makes time-to-listen grow
    with the database — on a history of a few hundred Runs that is minutes, and
    every caller that waits for the port (scripts/start-all.ps1, health checks)
    gives up long before the service is actually ready.  Running it as a
    background task keeps startup constant while the supervisor and cleanup loops
    wait for it before their first tick, so no Run is resumed while its stale
    lease is still present.

    Failures are logged rather than raised: the service is already accepting
    traffic at that point, and a stale projection is recoverable on the next
    restart, whereas a dead background task is not.
    """

    try:
        async with SessionLocal() as session:
            # Native Muteki can emit muteki.run_finished and tear down its Sandbox
            # before the outer lifecycle commit. Reconcile that durable handoff
            # before lease cleanup changes RUNNING to PAUSED_RECOVERY.
            persisted_runs = list((await session.scalars(select(SolveRun))).all())
            await run_supervisor.reconcile_muteki_native_terminals(session, persisted_runs)
            await run_attempt_service.reconcile_startup(session)
            # Durable before the per-Run sweep starts, so a rollback of one Run's
            # projection cannot undo the native-terminal handoff or the lease
            # recovery that just happened.
            await session.commit()
            # Reconcile every persisted Run after process restart.  This also
            # removes leases left by a previously terminated process and repairs
            # phase projections before the first resume request arrives.
            persisted_runs = list((await session.scalars(select(SolveRun))).all())
            persisted_run_ids = [str(run.id) for run in persisted_runs]
            reconciled_runs = 0
            for run_id in persisted_run_ids:
                # Per-Run guard: one row the finalizer cannot make sense of must
                # not cost the rest of the history its reconciliation.
                try:
                    # Re-read by id rather than closing over the objects above:
                    # a rollback expires every instance in the session, and an
                    # expired attribute access raises under asyncio ("greenlet
                    # spawn was called"), so a single failure would take out
                    # every Run after it -- the exact outcome this guard exists
                    # to prevent.
                    run = await session.get(SolveRun, run_id, populate_existing=True)
                    if run is None:
                        continue
                    await run_finalizer.reconcile(session, run)
                    # Committed per Run rather than once at the end.  The history
                    # grows without bound, and a single transaction over all of it
                    # holds row locks for the whole sweep -- long enough that the
                    # supervisor's own writes time out with MySQL 1205 and their
                    # work is lost.  Each Run is independent and idempotent, so
                    # there is nothing to gain by batching them.
                    await session.commit()
                    reconciled_runs += 1
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await session.rollback()
                    logger.exception("Run reconciliation failed run_id=%s", run_id)
            if persisted_run_ids:
                logger.info(
                    "Run reconciliation swept %d of %d persisted runs",
                    reconciled_runs,
                    len(persisted_run_ids),
                )
            # A prior process may have terminalized the SQL Run before its native
            # Muteki task/container finished.  Reconcile those exact run-scoped
            # Sandboxes at startup using the official idempotent teardown API.
            # Re-read here for the same expiry reason as above -- and it also
            # picks up the statuses the sweep just wrote.
            persisted_runs = list((await session.scalars(select(SolveRun))).all())
            await run_supervisor.reap_terminal_muteki_containers(persisted_runs)
            recovery = await deterministic_controller.reconcile_startup(session)
            for run_id in recovery.get("run_ids", []):
                run = await session.get(SolveRun, run_id)
                status = parse_run_status(run.status) if run is not None else None
                if run and (status is None or status not in TERMINAL):
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
        # Requeue persisted work after a process restart before accepting new
        # in-memory wakeups.
        await run_supervisor.recover_pending_continuations()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "Startup reconciliation failed; the API is up but durable Run state may be stale"
        )


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
# Mounted without the /api/v1 prefix on purpose: Claude clients append
# /v1/messages to their configured base URL, so the path is part of the contract
# and cannot be relocated.
app.include_router(claude_proxy_router)
# Same reasoning as the Claude proxy above: codex appends /responses to the
# provider base_url it is given, so the path is fixed by the client, not by us.
app.include_router(codex_proxy_router)
