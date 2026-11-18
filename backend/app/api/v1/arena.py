"""Arena integration endpoints: connection maintenance, catalogue, and the solve loop.

Two API surfaces, both here
---------------------------
The arena exposes the same information through two unrelated surfaces, and this
router deliberately uses whichever is correct per endpoint rather than picking
one:

* **REST** (``/api/open/v1/user``) -- exactly two read routes.  Kept for the
  catalogue listing, which is the one thing it does without a session handshake.
* **MCP** (``/api/ai/v1/mcp``) -- the twelve ``ctf2_*`` tools, including the
  environment lifecycle and flag submission.  Everything else goes through here.

Write scope
-----------
Most endpoints remain read-only.  The solve endpoint is the exception and is the
only place that can occupy the platform's single environment slot; it destroys
that environment on every path, including failure, because a leaked slot blocks
every later attempt with an error that points somewhere else entirely.
"""

import asyncio
import logging
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_session
from app.core.exceptions import DomainError
from app.models.arena import ArenaConnection, ArenaSolveQueueItem, ArenaSubmission
from app.models.model_config import ModelConfig
from app.schemas.arena import (
    ArenaConnectionRead,
    ArenaConnectionWrite,
    ArenaEnqueueByIdRequest,
    ArenaEnqueueRequest,
    ArenaQueueBulkRequest,
    ArenaQueueItemRead,
    ArenaQueuePriorityRequest,
    ArenaSolveRequest,
    ArenaSubmissionRead,
)
from app.schemas.model_config import (
    OPENAI_WIRE_PROVIDER_TYPES,
    WORKER_ENGINE_TYPES,
    normalize_worker_engine_type,
    runtime_engine_type,
)
from app.services import arena_ledger
from app.services import arena_solve as arena_solve_service
from app.services.arena_client import (
    MAX_PAGE_SIZE,
    OFFSET_SUPPORTED,
    ArenaClient,
    arena_clients,
)
from app.services.arena_lease import (
    arena_lease_manager,
    reclaim_orphans,
    reclaim_persisted_lease,
)
from app.services.arena_mcp import arena_mcp_clients
from app.services.arena_queue import arena_queue_scheduler, parse_stop_at
from app.services.crypto import decrypt_api_key, encrypt_api_key

router = APIRouter(prefix="/arena", tags=["arena"])

logger = logging.getLogger(__name__)

#: Solve tasks live in memory.  A solution is minutes long and its result is a
#: report, not state the next request depends on, so persisting it would add a
#: table for something the events stream and the scorebook already cover.
_solve_tasks: dict[str, dict] = {}

#: How many finished solve reports to keep.  Bounded because the dict is
#: process-lifetime: a long session of single solves would otherwise grow it
#: without limit for results nothing will read again.
ARENA_SOLVE_TASK_HISTORY = 50

#: Task ids currently in flight.
#:
#: The lease is the real serialisation point, but the route takes it some awaits
#: into the task -- so two rapid ``POST /solve`` calls both pass the pre-flight
#: check, and the second dies with ``ARENA_LEASE_BUSY`` *inside a background
#: task*, after the HTTP call already answered 202.  The operator then sees a
#: successful request that produced nothing, which is the worst of both errors.
#: This flag moves the refusal to the caller where it can be acted on.
_solve_inflight: set[str] = set()


def _remember_solve_task(task_id: str, payload: dict) -> None:
    """Store a solve report, evicting the oldest finished ones past the cap.

    Never evicts a running task: its handle is the only way to read a result that
    is still being produced.
    """
    _solve_tasks[task_id] = payload
    while len(_solve_tasks) > ARENA_SOLVE_TASK_HISTORY:
        oldest = next(iter(_solve_tasks))
        if _solve_tasks[oldest].get("state") == "running":
            break
        del _solve_tasks[oldest]



def _read(item: ArenaConnection) -> ArenaConnectionRead:
    return ArenaConnectionRead(
        id=item.id,
        name=item.name,
        base_url=item.base_url,
        token_configured=bool(item.encrypted_token),
        enabled=item.enabled,
        requests_per_minute=item.requests_per_minute,
        max_concurrency=item.max_concurrency,
        last_probe_at=item.last_probe_at,
        last_probe_ok=item.last_probe_ok,
        profile_json=item.profile_json or {},
        default_model_config_id=item.default_model_config_id,
        default_engine_type=item.default_engine_type,
        default_solver_mode=item.default_solver_mode or "muteki",
        default_reason_model_config_id=item.default_reason_model_config_id,
        default_worker_engines=list(item.default_worker_engines or []),
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


async def _require(session: AsyncSession, connection_id: str) -> ArenaConnection:
    item = await session.get(ArenaConnection, connection_id)
    if not item:
        raise DomainError("ARENA_CONNECTION_NOT_FOUND", "Arena connection not found.", status_code=404)
    return item


async def _validate_model_selection(
    session: AsyncSession, payload: ArenaConnectionWrite
) -> None:
    """Reject a runtime selection that ``create_run`` would later refuse.

    The same rules are enforced again when a Run is built, so this is not the
    safety net -- it is a better error.  A connection saved with, say, a Worker
    engine that its model does not run on would otherwise look fine here and then
    fail at solve time, hours into a batch, where the cause is far from the edit
    that introduced it.

    Validating against the *stored* provider type, not the name in the request, is
    deliberate: a ``local_proxy`` config executes on the ``openai_compatible``
    engine, so comparing the requested engine to ``provider_type`` verbatim would
    reject a valid Worker.
    """

    for entry in payload.default_worker_engines:
        engine_type = normalize_worker_engine_type(entry.engine_type)
        if not engine_type or engine_type not in WORKER_ENGINE_TYPES:
            raise DomainError(
                "ARENA_WORKER_ENGINE_INVALID",
                f"Worker engine {entry.engine_type!r} is not a supported execution path.",
                status_code=422,
            )
        if not entry.model_config_id:
            raise DomainError(
                "ARENA_WORKER_MODEL_REQUIRED",
                f"Worker engine {entry.engine_type!r} needs a model configuration.",
                status_code=422,
            )
        config = await session.get(ModelConfig, entry.model_config_id)
        if not config or not config.enabled:
            raise DomainError(
                "ARENA_WORKER_MODEL_INVALID",
                "Every Worker requires an enabled model configuration.",
                status_code=422,
            )
        if runtime_engine_type(config.provider_type) != engine_type:
            raise DomainError(
                "ARENA_WORKER_MODEL_MISMATCH",
                f"Worker engine {entry.engine_type!r} cannot run model {config.name!r} "
                f"(it executes on {runtime_engine_type(config.provider_type)!r}).",
                status_code=422,
            )
        roles = (config.capabilities_json or {}).get("roles", ["worker"])
        if "worker" not in roles:
            raise DomainError(
                "ARENA_WORKER_ROLE_INVALID",
                f"Model {config.name!r} is not enabled for Worker execution.",
                status_code=422,
            )

    if payload.default_reason_model_config_id:
        reason = await session.get(ModelConfig, payload.default_reason_model_config_id)
        if not reason or not reason.enabled:
            raise DomainError(
                "ARENA_REASON_MODEL_INVALID",
                "The Coordinator reasoning model must exist and be enabled.",
                status_code=422,
            )
        if reason.provider_type not in OPENAI_WIRE_PROVIDER_TYPES:
            # The Coordinator is driven over the OpenAI wire protocol, so a CLI
            # engine cannot serve it however it is configured.
            raise DomainError(
                "ARENA_REASON_MODEL_PROVIDER_INVALID",
                f"Coordinator reasoning requires an OpenAI-wire model; {reason.name!r} "
                f"uses {reason.provider_type!r}.",
                status_code=422,
            )
        roles = (reason.capabilities_json or {}).get("roles", ["worker"])
        if "coordinator_reason" not in roles:
            raise DomainError(
                "ARENA_REASON_MODEL_ROLE_INVALID",
                f"Model {reason.name!r} is not enabled for Coordinator reasoning.",
                status_code=422,
            )


def _token_of(item: ArenaConnection) -> str | None:
    if not item.encrypted_token:
        return None
    try:
        return decrypt_api_key(item.encrypted_token)
    except DomainError:
        return None


async def _client_for(session: AsyncSession, item: ArenaConnection):
    # Always go through the registry: the platform rate-limits per agent, so a
    # second client for the same connection would be a second pace limiter
    # against one shared quota.
    return await arena_clients.get(item, _token_of(item))


async def _mcp_for(session: AsyncSession, item: ArenaConnection):
    """The MCP client for this connection -- the only route to ten of the tools."""
    return await arena_mcp_clients.get(item, _token_of(item))


@router.get("/connections")
async def list_connections(session: AsyncSession = Depends(get_session)) -> dict:
    items = await arena_ledger.connection_rows(session)
    return {"data": [_read(item) for item in items]}


@router.post("/connections", status_code=201)
async def create_connection(
    payload: ArenaConnectionWrite, session: AsyncSession = Depends(get_session)
) -> dict:
    if await session.scalar(select(ArenaConnection.id).where(ArenaConnection.name == payload.name)):
        raise DomainError(
            "ARENA_CONNECTION_NAME_TAKEN", "An arena connection with this name exists.", status_code=409
        )
    await _validate_model_selection(session, payload)
    item = ArenaConnection(
        name=payload.name,
        base_url=str(payload.base_url).rstrip("/"),
        encrypted_token=encrypt_api_key(payload.token) if payload.token else None,
        enabled=payload.enabled,
        requests_per_minute=payload.requests_per_minute,
        max_concurrency=payload.max_concurrency,
        default_model_config_id=payload.default_model_config_id,
        default_engine_type=payload.default_engine_type,
        default_solver_mode=payload.default_solver_mode,
        default_reason_model_config_id=payload.default_reason_model_config_id,
        default_worker_engines=[item.model_dump() for item in payload.default_worker_engines],
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return {"data": _read(item)}


@router.put("/connections/{connection_id}")
async def update_connection(
    connection_id: str, payload: ArenaConnectionWrite, session: AsyncSession = Depends(get_session)
) -> dict:
    item = await _require(session, connection_id)
    clash = await session.scalar(
        select(ArenaConnection.id).where(
            ArenaConnection.name == payload.name, ArenaConnection.id != item.id
        )
    )
    if clash:
        raise DomainError(
            "ARENA_CONNECTION_NAME_TAKEN", "An arena connection with this name exists.", status_code=409
        )
    await _validate_model_selection(session, payload)
    item.name = payload.name
    item.base_url = str(payload.base_url).rstrip("/")
    # A blank token keeps the stored one: renaming a connection must not force the
    # credential to be pasted again, and an explicit clear is a separate concern.
    if payload.token:
        item.encrypted_token = encrypt_api_key(payload.token)
    item.enabled = payload.enabled
    item.requests_per_minute = payload.requests_per_minute
    item.max_concurrency = payload.max_concurrency
    item.default_model_config_id = payload.default_model_config_id
    item.default_engine_type = payload.default_engine_type
    item.default_solver_mode = payload.default_solver_mode
    item.default_reason_model_config_id = payload.default_reason_model_config_id
    item.default_worker_engines = [entry.model_dump() for entry in payload.default_worker_engines]
    await session.commit()
    await session.refresh(item)
    # Drop the cached client so the new token and pace limits take effect at once.
    arena_clients.forget(item.id)
    return {"data": _read(item)}


@router.delete("/connections/{connection_id}", status_code=204)
async def delete_connection(
    connection_id: str, session: AsyncSession = Depends(get_session)
) -> Response:
    item = await _require(session, connection_id)
    submissions = list(
        (
            await session.scalars(
                select(ArenaSubmission).where(ArenaSubmission.connection_id == item.id)
            )
        ).all()
    )
    for submission in submissions:
        await session.delete(submission)
    await session.flush()
    await session.delete(item)
    await session.commit()
    arena_clients.forget(connection_id)
    return Response(status_code=204)


@router.post("/connections/{connection_id}/probe")
async def probe_connection(
    connection_id: str, session: AsyncSession = Depends(get_session)
) -> dict:
    """Run ``whoami`` and cache the result.  The only call that leaves the host."""
    item = await _require(session, connection_id)
    client = await _client_for(session, item)
    checks: list[dict] = []
    try:
        profile = await client.probe()
        item.last_probe_ok = True
        item.profile_json = profile
        checks.append({"name": "whoami", "ok": True, "message": "Arena accepted the token."})
    except DomainError as error:
        item.last_probe_ok = False
        checks.append({"name": "whoami", "ok": False, "message": error.message})
    except Exception as error:  # noqa: BLE001 - probe must never raise at the operator
        item.last_probe_ok = False
        checks.append({"name": "whoami", "ok": False, "message": str(error)[:200]})
    item.last_probe_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(item)
    return {"data": {"connection": _read(item), "checks": checks}}


@router.get("/status")
async def arena_status(session: AsyncSession = Depends(get_session)) -> dict:
    """One page of standing information.

    The shape mirrors ``/readiness``: a list of ``{name, ok, message}``.  Reusing
    that shape means the status view needs no new rendering concept, and an
    operator who has read the readiness page already knows how to read this one.
    """
    items = await arena_ledger.connection_rows(session)
    checks: list[dict] = []
    if not items:
        checks.append(
            {"name": "connection", "ok": False, "message": "No arena connection configured."}
        )
    for item in items:
        ok = bool(item.last_probe_ok)
        message = (
            f"Last probe succeeded ({item.last_probe_at.isoformat()})"
            if ok and item.last_probe_at
            else "Last probe failed or has never run."
            if item.last_probe_at
            else "Never probed."
        )
        checks.append({"name": f"connection:{item.name}", "ok": ok, "message": message})
        # Surface the platform's own numbers instead of a guess.  max_environments
        # in particular is what makes the "capacity 1" constraint a fact rather
        # than an assumption, and read/write rpm are the real pace limits -- the
        # local throttle in the connection row is only a conservative default.
        quotas = item.profile_json.get("quotas") if isinstance(item.profile_json, dict) else None
        if isinstance(quotas, dict) and quotas:
            checks.append(
                {
                    "name": f"quota:{item.name}",
                    "ok": True,
                    "message": (
                        f"environments {quotas.get('active_environments', '?')}"
                        f"/{quotas.get('max_environments', '?')}; "
                        f"read {quotas.get('read_requests_per_minute', '?')}/min, "
                        f"write {quotas.get('write_requests_per_minute', '?')}/min"
                    ),
                }
            )
        tools = item.profile_json.get("allowed_tools") if isinstance(item.profile_json, dict) else None
        if isinstance(tools, list):
            checks.append(
                {
                    "name": f"tools:{item.name}",
                    "ok": True,
                    "message": (
                        f"{len(tools)} MCP tools available at /api/ai/v1/mcp; "
                        f"2 also readable over REST"
                    ),
                }
            )
    # The environment slot is a platform-wide capacity of one, so its state is the
    # single most useful thing on this page: whatever holds it blocks every other
    # attempt.  Reported from the local lease, and cross-checked against the
    # platform count when a probe has been run, so a leaked slot is visible rather
    # than inferred from unrelated failures.
    lease = arena_lease_manager.current
    if lease and not lease.released:
        checks.append(
            {
                "name": "environment_slot",
                "ok": True,
                "message": (
                    f"Held by this console: {lease.access_url or lease.environment_id} "
                    f"(challenge {lease.challenge_id})"
                ),
            }
        )
    else:
        checks.append(
            {
                "name": "environment_slot",
                "ok": True,
                "message": "Idle; capacity 1.",
            }
        )
    stats = await arena_ledger.aggregate_stats(session)
    scheduler = arena_queue_scheduler.health()
    counts: dict[str, int] = {}
    for status_value in (await session.scalars(select(ArenaSolveQueueItem.status))).all():
        key = str(status_value)
        counts[key] = counts.get(key, 0) + 1
    backlog = counts.get("PENDING", 0)
    # The batch is the part of this page that can fail *silently*: a stopped
    # scheduler and a scheduler waiting on a slow challenge are indistinguishable
    # from a queue that simply is not moving, and a batch spends hours in that
    # state with nobody watching.  These two checks are what make the difference
    # between "no progress" and "no progress because the loop is gone".
    if scheduler["running"]:
        item_age = scheduler.get("current_item_age_seconds")
        # Stalled means "the loop stopped iterating with nothing to blame".  A
        # long tick age while an item is in flight is a long item, not a wedge --
        # the loop is inside ``_process`` and simply is not ticking.  Reporting
        # that as stalled made every healthy solve past twenty minutes look
        # broken, which is how an operator learns to ignore the page.
        if item_age is not None:
            near_watchdog = item_age > scheduler["watchdog_seconds"]
            checks.append(
                {
                    "name": "scheduler",
                    "ok": not near_watchdog,
                    "message": (
                        f"One item has been running for {item_age:.0f}s "
                        f"(watchdog {scheduler['watchdog_seconds']:.0f}s) -- "
                        "about to be timed out and requeued."
                        if near_watchdog
                        else f"Running; 1 item in flight for {item_age:.0f}s, "
                        f"{backlog} challenge(s) waiting."
                    ),
                }
            )
        else:
            tick_age = scheduler["last_tick_age_seconds"]
            stalled = tick_age is not None and tick_age > scheduler["watchdog_seconds"]
            checks.append(
                {
                    "name": "scheduler",
                    "ok": not stalled,
                    "message": (
                        f"Stalled: no scheduler tick for {tick_age:.0f}s and no item "
                        f"in flight (watchdog {scheduler['watchdog_seconds']:.0f}s)."
                        if stalled
                        else f"Running; {backlog} challenge(s) waiting."
                    ),
                }
            )
        if scheduler["consecutive_failures"]:
            checks.append(
                {
                    "name": "scheduler_errors",
                    "ok": False,
                    "message": (
                        f"{scheduler['consecutive_failures']} consecutive backlog read "
                        f"failure(s): {scheduler['last_error']}"
                    ),
                }
            )
    elif backlog:
        checks.append(
            {
                "name": "scheduler",
                "ok": False,
                "message": (
                    f"Stopped with {backlog} challenge(s) waiting. "
                    "Start the scheduler to continue the batch."
                ),
            }
        )
    else:
        checks.append({"name": "scheduler", "ok": True, "message": "Idle; no backlog."})

    # An item left RUNNING with nothing processing it is invisible to the
    # dispatcher (which only reads PENDING), so it would sit in the list looking
    # like work in progress forever.  Reported here, repaired by
    # ``POST /queue/recover``.
    orphan_running = counts.get("RUNNING", 0) - (1 if scheduler["current_item_id"] else 0)
    if orphan_running > 0:
        checks.append(
            {
                "name": "stranded_items",
                "ok": False,
                "message": (
                    f"{orphan_running} item(s) stuck in RUNNING with no scheduler "
                    "processing them; use /arena/queue/recover."
                ),
            }
        )
    return {
        "data": {
            "checks": checks,
            "connections": [_read(item) for item in items],
            "stats": stats,
            "scheduler": scheduler,
            "queue_counts": counts,
        }
    }


@router.get("/challenges")
async def arena_challenges(
    connection_id: str,
    category: str | None = None,
    limit: int = MAX_PAGE_SIZE,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The catalogue page.

    Categories are discovered, never enumerated from a constant: the platform's
    ``category`` label is free-form and some rows carry a chapter name, so a fixed
    list would silently hide challenges.
    """
    item = await _require(session, connection_id)
    client = await _client_for(session, item)
    page = await client.list_challenges(category=category, limit=limit)
    return {
        "data": {
            **page,
            "categories": ArenaClient.categories(page["items"]),
            # Stated in the payload, not just the docstring: a caller that sees a
            # full page needs to know it cannot page to the end.
            "paging_supported": OFFSET_SUPPORTED,
        }
    }


@router.get("/stats")
async def arena_stats(
    connection_id: str | None = None, session: AsyncSession = Depends(get_session)
) -> dict:
    """The category x attribution matrix -- the only place a real claim can be read."""
    stats = await arena_ledger.aggregate_stats(session, connection_id=connection_id)

    # Resolve model names so the matrix is readable without cross-referencing ids.
    names = {
        item.id: item.name
        for item in (await session.scalars(select(ModelConfig))).all()
    }
    for key, value in stats["by_model"].items():
        value["label"] = names.get(key, key)
    return {"data": stats}


@router.get("/submissions")
async def arena_submissions(
    connection_id: str | None = None,
    limit: int = 200,
    session: AsyncSession = Depends(get_session),
) -> dict:
    statement = select(ArenaSubmission)
    if connection_id:
        statement = statement.where(ArenaSubmission.connection_id == connection_id)
    items = list((await session.scalars(statement)).all())
    items.sort(key=lambda item: item.submitted_at, reverse=True)
    payload = [
        ArenaSubmissionRead(
            id=item.id,
            connection_id=item.connection_id,
            platform_challenge_id=item.platform_challenge_id,
            challenge_name=item.challenge_name,
            category=item.category,
            verdict=item.verdict,
            submitted_at=item.submitted_at,
            model_config_id=item.model_config_id,
            engine_key=item.engine_key,
            token_cost=item.token_cost,
        )
        for item in items[: max(1, min(limit, 1000))]
    ]
    return {"data": payload}


# -- MCP-backed reads ---------------------------------------------------------
# These go through /api/ai/v1/mcp rather than the REST surface, which does not
# expose grounds or challenge detail at all.


@router.get("/grounds")
async def arena_grounds(
    connection_id: str, limit: int = 50, session: AsyncSession = Depends(get_session)
) -> dict:
    """Practice grounds (the arenas within the arena)."""
    item = await _require(session, connection_id)
    client = await _mcp_for(session, item)
    payload = await client.list_practice_grounds(limit=max(1, min(limit, 100)))
    return {"data": payload}


@router.get("/mcp-challenges")
async def arena_mcp_challenges(
    connection_id: str,
    practice_ground_id: str,
    category: str | None = None,
    difficulty: str | None = None,
    unsolved_only: bool = False,
    page: int | None = None,
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Challenge list inside one ground, with attachment/container flags.

    ``has_container`` is surfaced prominently because it decides whether the
    single environment slot is needed at all: an attachment-only challenge can be
    solved without ever leasing one.

    ``page``/``difficulty``/``unsolved_only`` are forwarded rather than filtered
    here: a ground can hold thousands of challenges, so filtering one page of 100
    on this side would answer "no match" for everything past page 1 -- a wrong
    answer that looks exactly like a valid one.
    """
    item = await _require(session, connection_id)
    client = await _mcp_for(session, item)
    payload = await client.list_practice_challenges(
        practice_ground_id=practice_ground_id,
        category=category,
        difficulty=difficulty,
        limit=max(1, min(limit, 100)),
        page=page,
        unsolved_only=unsolved_only,
    )
    items = payload.get("items") if isinstance(payload, dict) else None
    rows = [row for row in items if isinstance(row, dict)] if isinstance(items, list) else []
    return {
        "data": {
            "items": rows,
            "total": payload.get("total") if isinstance(payload, dict) else None,
            "page": payload.get("page") if isinstance(payload, dict) else None,
            "total_page": payload.get("total_page") if isinstance(payload, dict) else None,
            "container_count": sum(1 for row in rows if row.get("has_container")),
            "attachment_count": sum(1 for row in rows if row.get("has_attachments")),
        }
    }


@router.get("/mcp-challenges/{challenge_id}")
async def arena_mcp_challenge(
    challenge_id: str,
    connection_id: str,
    practice_ground_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """One challenge in full, straight from the platform (nothing persisted)."""
    item = await _require(session, connection_id)
    client = await _mcp_for(session, item)
    payload = await client.get_practice_challenge(
        practice_ground_id=practice_ground_id, challenge_id=challenge_id
    )
    return {"data": payload}


@router.get("/orphans")
async def arena_orphans(
    connection_id: str, session: AsyncSession = Depends(get_session)
) -> dict:
    """Reconcile the local slot against the platform's own count.

    A crash between start and destroy leaves the single slot occupied with no
    in-process record, and the symptom is every later attempt failing for an
    unrelated-looking reason.  This is the endpoint that turns that into a fact.
    """
    item = await _require(session, connection_id)
    client = await _mcp_for(session, item)
    return {"data": await reclaim_orphans(client)}


@router.post("/orphans/reclaim")
async def arena_orphans_reclaim(
    connection_id: str,
    practice_ground_id: str | None = None,
    challenge_id: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Actively destroy a leaked environment, then re-read the platform's count.

    ``GET /orphans`` can only report a leak -- the platform publishes no "list my
    environments" tool, so naming the challenge was on the operator.  The lease
    breadcrumb (written as the environment is leased) supplies that name, which
    is what makes this a one-click repair instead of a guessing game.  The
    explicit parameters are the override for a record that is missing or wrong.

    Read-only until the caller asks: this is a POST because it writes to the
    platform, and it is not called automatically on the read path.
    """
    item = await _require(session, connection_id)
    client = await _mcp_for(session, item)
    result = await reclaim_persisted_lease(
        client,
        connection_id=item.id,
        practice_ground_id=practice_ground_id,
        challenge_id=challenge_id,
    )
    # Re-read the platform's own count rather than inferring success from the
    # delete call: the only answer that matters is whether the slot is free now.
    return {"data": {**result, "orphans": await reclaim_orphans(client)}}


# -- the solve loop -----------------------------------------------------------


@router.post("/solve", status_code=202)
async def arena_solve(
    payload: ArenaSolveRequest, session: AsyncSession = Depends(get_session)
) -> dict:
    """Run one challenge end to end: read, lease, solve, submit, destroy.

    Returns immediately with a task handle.  A solution takes minutes -- it leases
    an environment, waits for the target, and runs a full agent solve -- so
    holding the HTTP request open would be wrong; the caller polls
    ``/arena/solve/{task_id}``.

    Exactly one solve may run at a time, enforced by the lease rather than by this
    handler: a second concurrent call is refused by the manager with
    ``ARENA_LEASE_BUSY``, which is the platform's actual constraint rather than an
    arbitrary local limit.
    """
    connections = await arena_ledger.connection_rows(session)
    connection = next((row for row in connections if row.enabled), None)
    if connection is None:
        raise DomainError(
            "ARENA_CONNECTION_MISSING",
            "No enabled arena connection is configured.",
            status_code=422,
        )
    # Mirror the service's fallback: a global default in system settings is a
    # valid model source, so refusing here on the connection value alone would
    # reject a solve that would actually succeed.
    if not connection.default_model_config_id and not get_settings().default_model_config_id:
        raise DomainError(
            "ARENA_DEFAULT_MODEL_MISSING",
            "Set this connection's default model, or a global default in system settings, "
            "before starting a solve.",
            status_code=422,
        )
    if arena_lease_manager.current and not arena_lease_manager.current.released:
        raise DomainError(
            "ARENA_LEASE_BUSY",
            "A solve is already running; the platform allows one environment at a time.",
            status_code=409,
        )
    if _solve_inflight:
        raise DomainError(
            "ARENA_SOLVE_BUSY",
            "Another single-challenge solve is still starting or running. "
            "Wait for it to finish, or use the batch queue.",
            status_code=409,
        )

    task_id = uuid4().hex
    _solve_inflight.add(task_id)
    _remember_solve_task(task_id, {"state": "running", "started_at": datetime.now(UTC).isoformat()})

    async def _run() -> None:
        # A fresh session: the request's session is closed the moment this handler
        # returns, and the solve outlives it by minutes.
        from app.core.database import SessionLocal

        try:
            async with SessionLocal() as task_session:
                task_connection = await task_session.get(ArenaConnection, connection.id)
                outcome = await arena_solve_service.solve_one(
                    task_session,
                    connection=task_connection,
                    practice_ground_id=payload.practice_ground_id,
                    challenge_id=payload.challenge_id,
                    start_run_fn=arena_solve_service.start_run_for_arena,
                )
            _remember_solve_task(
                task_id, {"state": "finished", "outcome": outcome.describe()}
            )
        except Exception as error:  # noqa: BLE001 - surfaced through the task handle
            # The handle only carries ``str(error)``, which for an unpacking
            # failure is "too many values to unpack (expected 3)" -- accurate and
            # useless.  The traceback is the only thing that says *where*.
            logger.exception("arena solve %s failed", task_id)
            _remember_solve_task(
                task_id,
                {"state": "failed", "error": f"{type(error).__name__}: {error}"[:1000]},
            )
        finally:
            # Released on every path, including cancellation: a flag stuck on
            # would refuse every later single solve until the process restarted.
            _solve_inflight.discard(task_id)

    asyncio.create_task(_run(), name=f"arena-solve-{task_id}")
    return {"data": {"task_id": task_id, "state": "running", "connection_id": connection.id}}


@router.get("/solve/{task_id}")
async def arena_solve_status(task_id: str) -> dict:
    task = _solve_tasks.get(task_id)
    if task is None:
        raise DomainError("ARENA_TASK_NOT_FOUND", "Unknown solve task.", status_code=404)
    return {"data": task}


# -- batch queue --------------------------------------------------------------


def _queue_read(item: ArenaSolveQueueItem) -> ArenaQueueItemRead:
    return ArenaQueueItemRead(
        id=item.id,
        connection_id=item.connection_id,
        practice_ground_id=item.practice_ground_id,
        platform_challenge_id=item.platform_challenge_id,
        challenge_name=item.challenge_name,
        category=item.category,
        difficulty=item.difficulty,
        has_container=item.has_container,
        status=item.status,
        priority=item.priority,
        verdict=item.verdict,
        attempt_count=item.attempt_count,
        outcome_count=item.outcome_count,
        last_error=item.last_error,
        run_id=item.run_id,
        started_at=item.started_at,
        finished_at=item.finished_at,
        created_at=item.created_at,
    )


#: The catalogue is paginated, and the platform accepts 100 rows per page.
ARENA_CATALOGUE_PAGE_SIZE = 100

#: A ground can hold thousands of challenges (BUUCTF is 3244, i.e. 33 pages).
#: Reading it page by page is cheap -- one page is one read and the platform
#: allows 120 reads a minute -- but an unbounded loop against a catalogue that
#: grows is not, so the scan stops here.  40 pages covers every ground we have
#: seen with room to spare, and the response reports when it was hit.
ARENA_CATALOGUE_MAX_PAGES = 40


async def _catalogue_scan(
    client,
    *,
    practice_ground_id: str,
    wanted: int,
    category: str | None = None,
    difficulty: str | None = None,
    unsolved_only: bool = False,
    order: str = "as_listed",
) -> tuple[list[dict], int, bool]:
    """Page through a ground's catalogue until enough usable rows are in hand.

    Filtering happens **on the platform**, not here, and that is the point: a
    single page holds 100 of BUUCTF's 3244 challenges, so a local ``category``
    filter would answer "no match" for every challenge that happens to sit past
    page 1 -- a wrong answer that looks exactly like a valid one.

    Stops as soon as ``wanted`` rows of the preferred kind have been seen, so the
    common case costs one page rather than thirty-three:

    * ``as_listed``     -- stop at ``wanted`` matches.
    * ``container_first``/``offline_first`` -- stop when ``wanted`` rows of the
      preferred kind are in hand, because the sort that follows is stable and the
      first ``wanted`` preferred rows *are* the answer.

    Returns ``(rows, total, truncated)``; ``truncated`` is true when the page cap
    was reached before the catalogue ran out, so a caller can say so instead of
    implying it saw everything.
    """

    matched: list[dict] = []
    total = 0
    truncated = False
    preferred = None if order == "as_listed" else (order == "container_first")
    page = 1
    while page <= ARENA_CATALOGUE_MAX_PAGES:
        payload = await client.list_practice_challenges(
            practice_ground_id=practice_ground_id,
            category=category,
            difficulty=difficulty,
            limit=ARENA_CATALOGUE_PAGE_SIZE,
            page=page,
            unsolved_only=unsolved_only,
        )
        if not isinstance(payload, dict):
            break
        page_rows = [row for row in (payload.get("items") or []) if isinstance(row, dict)]
        rows = page_rows
        if page == 1:
            total = int(payload.get("total") or 0)
        # Belt and braces: the filter is applied upstream, but a platform that
        # stopped honouring it would otherwise queue the wrong challenges.
        if category:
            rows = [row for row in rows if str(row.get("category") or "") == category]
        if difficulty:
            rows = [row for row in rows if str(row.get("difficulty") or "") == difficulty]
        if unsolved_only:
            rows = [row for row in rows if not row.get("is_solved")]
        matched.extend(rows)

        if preferred is None:
            enough = len(matched) >= wanted
        else:
            enough = sum(1 for row in matched if bool(row.get("has_container")) == preferred) >= wanted
        if enough:
            break

        # Judged on the *raw* page, not on the locally filtered one: a page that
        # the local filter emptied is not evidence that the catalogue ended, and
        # treating it as such would silently stop the scan early.
        total_page = int(payload.get("total_page") or 0)
        if (total_page and page >= total_page) or len(page_rows) < ARENA_CATALOGUE_PAGE_SIZE:
            break
        page += 1
    else:
        truncated = True
    return matched, total, truncated


@router.post("/queue/enqueue")
async def arena_queue_enqueue(
    payload: ArenaEnqueueRequest, session: AsyncSession = Depends(get_session)
) -> dict:
    """Add challenges to the batch backlog.

    Reads the catalogue -- paging through it as needed -- and insert-or-ignores
    into the queue.  Idempotent by design: the table's unique constraint means
    re-enqueueing a challenge that is already queued (or already solved) is a
    no-op rather than a second attempt that would burn the environment slot
    again.
    """
    rows = await arena_ledger.connection_rows(session)
    connection = next((row for row in rows if row.enabled), None)
    if connection is None:
        raise DomainError(
            "ARENA_CONNECTION_MISSING", "No enabled arena connection is configured.", status_code=422
        )
    client = await _mcp_for(session, connection)
    items, catalogue_total, truncated = await _catalogue_scan(
        client,
        practice_ground_id=payload.practice_ground_id,
        wanted=payload.limit,
        category=payload.category,
        difficulty=payload.difficulty,
        unsolved_only=payload.skip_solved,
        order=payload.order,
    )

    if payload.order == "offline_first":
        items.sort(key=lambda row: bool(row.get("has_container")))
    elif payload.order == "container_first":
        items.sort(key=lambda row: not bool(row.get("has_container")))

    selected = items[: payload.limit]

    # The scheduler orders by ``priority`` alone, so the batch's intent has to be
    # written into the rows here -- a runtime sort key would be lost the moment
    # the dispatcher re-read the queue.  A container challenge is the thing that
    # can actually score, so ``container_first`` gives it the lower (earlier)
    # number; ``as_listed`` keeps both at the default and lets age decide.
    priority_by_container = {
        "container_first": (0, 100),
        "offline_first": (100, 0),
    }.get(payload.order)

    existing = {
        (row.practice_ground_id, row.platform_challenge_id)
        for row in (
            await session.scalars(
                select(ArenaSolveQueueItem).where(
                    ArenaSolveQueueItem.connection_id == connection.id
                )
            )
        ).all()
    }

    added = 0
    skipped = 0
    for row in selected:
        key = (payload.practice_ground_id, str(row.get("id")))
        if key in existing:
            skipped += 1
            continue
        has_container = bool(row.get("has_container"))
        session.add(
            ArenaSolveQueueItem(
                connection_id=connection.id,
                practice_ground_id=payload.practice_ground_id,
                platform_challenge_id=str(row.get("id")),
                challenge_name=str(row.get("name") or "")[:200],
                category=str(row.get("category") or "")[:60],
                difficulty=str(row.get("difficulty") or "")[:40],
                has_container=has_container,
                priority=(
                    priority_by_container[0 if has_container else 1]
                    if priority_by_container
                    else 100
                ),
            )
        )
        added += 1
    await session.commit()
    return {
        "data": {
            "added": added,
            "skipped": skipped,
            "matched": len(items),
            "selected": len(selected),
            "order": payload.order,
            # Surfaced so a caller can tell "the ground has nothing left" apart
            # from "the scan stopped early" -- they look identical otherwise.
            "catalogue_total": catalogue_total,
            "scan_truncated": truncated,
        }
    }


@router.post("/queue/enqueue-ids")
async def arena_queue_enqueue_ids(
    payload: ArenaEnqueueByIdRequest, session: AsyncSession = Depends(get_session)
) -> dict:
    """Queue specific challenges, reading each one's metadata from the platform.

    The metadata is fetched rather than supplied by the caller so the queue row
    cannot disagree with the platform about whether a challenge needs an
    environment -- a mismatch there would silently break the offline/container
    ordering and, worse, could skip the lease for a challenge that needs one.

    Each id is read **directly** instead of being looked up in a catalogue page.
    Looking it up in one page of 100 was the old behaviour, and it made every
    challenge past page 1 unreachable: BUUCTF holds 3244, so naming any of them
    came back ``not_in_catalogue`` -- an answer that blamed the caller for a
    limit that was ours.  A per-id read has no such horizon, and it is also the
    fresher of the two: the point read reflects the platform's state now rather
    than whenever the page was assembled.
    """

    rows = await arena_ledger.connection_rows(session)
    connection = next((row for row in rows if row.enabled), None)
    if connection is None:
        raise DomainError(
            "ARENA_CONNECTION_MISSING", "No enabled arena connection is configured.", status_code=422
        )
    client = await _mcp_for(session, connection)

    existing = {
        (row.practice_ground_id, row.platform_challenge_id)
        for row in (
            await session.scalars(
                select(ArenaSolveQueueItem).where(
                    ArenaSolveQueueItem.connection_id == connection.id
                )
            )
        ).all()
    }

    added = 0
    skipped = 0
    unknown: list[str] = []
    for challenge_id in payload.challenge_ids:
        key = (payload.practice_ground_id, str(challenge_id))
        # Checked before the read so an already-queued id costs no platform call.
        if key in existing:
            skipped += 1
            continue
        try:
            detail = await client.get_practice_challenge(
                practice_ground_id=payload.practice_ground_id, challenge_id=str(challenge_id)
            )
        except DomainError:
            # The platform refusing an id (unknown, invisible, wrong ground) is a
            # fact about the caller's list, not a failure of the batch -- the
            # rest of the ids must still be queued.
            unknown.append(str(challenge_id))
            continue
        challenge = detail.get("challenge") if isinstance(detail, dict) else None
        if not isinstance(challenge, dict) or not challenge.get("id"):
            unknown.append(str(challenge_id))
            continue
        session.add(
            ArenaSolveQueueItem(
                connection_id=connection.id,
                practice_ground_id=payload.practice_ground_id,
                platform_challenge_id=str(challenge_id),
                challenge_name=str(challenge.get("name") or "")[:200],
                category=str(challenge.get("category") or "")[:60],
                difficulty=str(challenge.get("difficulty") or "")[:40],
                has_container=bool(challenge.get("has_container")),
                priority=payload.priority,
            )
        )
        added += 1
    await session.commit()
    return {
        "data": {
            "added": added,
            "skipped": skipped,
            "not_in_catalogue": len(unknown),
            # Named, not just counted: a caller that mistyped an id needs to know
            # which one, and a count alone cannot tell it.
            "unreadable_ids": unknown,
        }
    }


@router.get("/queue")
async def arena_queue_list(
    status: str | None = None, session: AsyncSession = Depends(get_session)
) -> dict:
    """The backlog, newest activity first, plus counts per status."""
    statement = select(ArenaSolveQueueItem)
    if status:
        statement = statement.where(ArenaSolveQueueItem.status == status)
    items = list((await session.scalars(statement)).all())
    items.sort(key=lambda item: (item.priority, item.created_at))
    all_items = list((await session.scalars(select(ArenaSolveQueueItem))).all())
    counts: dict[str, int] = {}
    for row in all_items:
        counts[row.status] = counts.get(row.status, 0) + 1
    return {
        "data": {
            "items": [_queue_read(item) for item in items],
            "counts": counts,
            "scheduler": arena_queue_scheduler.health(),
        }
    }


@router.post("/queue/start")
async def arena_queue_start(stop_at: str | None = None) -> dict:
    """Begin draining the backlog.

    An explicit action rather than automatic: a batch occupies the platform's one
    environment for hours, so it should not start because a process happened to
    restart.

    ``stop_at`` (optional) is the batch's wall-clock cutoff -- ISO-8601, or the
    local ``HH:MM`` an operator types at midnight.  Without it the batch drains
    the entire backlog, which is how a 06:30 deadline turned into a 07:50 finish.
    """
    try:
        deadline = parse_stop_at(stop_at)
    except ValueError as error:
        raise DomainError(
            "ARENA_QUEUE_STOP_AT_INVALID",
            str(error),
            {"stop_at": stop_at},
            422,
        ) from error
    return {"data": await arena_queue_scheduler.start(stop_at=deadline)}


@router.post("/queue/stop")
async def arena_queue_stop() -> dict:
    """Stop after the current item finishes.

    Not an immediate cancel: killing a solve mid-flight risks leaving the
    environment held, and the lease's cleanup runs on the way out, not on a hard
    interrupt.
    """
    return {"data": await arena_queue_scheduler.stop()}


@router.post("/queue/{item_id}/cancel")
async def arena_queue_cancel(item_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    item = await session.get(ArenaSolveQueueItem, item_id)
    if item is None:
        raise DomainError("ARENA_QUEUE_ITEM_NOT_FOUND", "Unknown queue item.", status_code=404)
    if item.status in {"SOLVED", "FAILED", "CANCELLED"}:
        raise DomainError(
            "ARENA_QUEUE_ITEM_FINISHED",
            f"Item is already {item.status} and cannot be cancelled.",
            status_code=409,
        )
    item.status = "CANCELLED"
    item.finished_at = datetime.now(UTC)
    await session.commit()
    return {"data": _queue_read(item)}


@router.post("/queue/recover")
async def arena_queue_recover() -> dict:
    """Return items stranded in ``RUNNING`` to the backlog.

    The scheduler does this for itself at startup and before its first tick; this
    exposes the same repair to an operator, because the one case it cannot cover
    is the scheduler never having been started at all -- and a stranded item is
    invisible to the dispatcher, so it would sit there looking like a stall.

    Bounded to the scheduler's own exclusion: the item it is actively processing
    is legitimately ``RUNNING`` and is left alone.
    """
    repaired = await arena_queue_scheduler.recover(
        exclude_item_id=arena_queue_scheduler.current_item_id
    )
    return {"data": {"repaired": repaired, "scheduler": arena_queue_scheduler.health()}}


@router.post("/queue/{item_id}/priority")
async def arena_queue_priority(
    item_id: str, payload: ArenaQueuePriorityRequest, session: AsyncSession = Depends(get_session)
) -> dict:
    """Move one item to the front or the back of the backlog.

    Expressed as a position rather than a number so reordering does not require
    the caller to know the scheduler's priority convention, and so two moves in a
    row cannot leave everything at the same value -- which would silently degrade
    the order to "whatever the database returns".
    """
    item = await session.get(ArenaSolveQueueItem, item_id)
    if item is None:
        raise DomainError("ARENA_QUEUE_ITEM_NOT_FOUND", "Unknown queue item.", status_code=404)
    if payload.position == "top":
        # One below the current best, not a constant: repeated "move to top" must
        # keep working instead of tying with the previous winner.
        lowest = await session.scalar(
            select(func.min(ArenaSolveQueueItem.priority)).where(
                ArenaSolveQueueItem.status == "PENDING"
            )
        )
        item.priority = int(lowest or 0) - 1
    else:
        highest = await session.scalar(select(func.max(ArenaSolveQueueItem.priority)))
        item.priority = int(highest or 0) + 1
    await session.commit()
    return {"data": _queue_read(item)}


#: Statuses that mean "this row is done"; the bulk actions that rearrange the
#: backlog must not resurrect or re-file them.
QUEUE_FINISHED_STATUSES = frozenset({"SOLVED", "UNSOLVED", "FAILED", "CANCELLED"})


def _apply_queue_bulk_action(
    item: ArenaSolveQueueItem, action: str, *, top_priority: int | None = None
) -> tuple[str | None, bool]:
    """Mutate one queue row in place.  Returns ``(skip_reason, should_delete)``.

    Split out of the route so the eligibility rules can be tested without a
    database -- these are exactly the rules that decide whether a "clear the
    failures" click silently does nothing or does the wrong thing.

    Three guards carry real weight:

    * A ``RUNNING`` row is never retried or deleted.  Deleting it would lose the
      outcome of an environment that has already been leased and paid for, and
      retrying it would race the solve that currently owns it.
    * ``retry`` resets ``attempt_count``.  Leaving it at the cap would make the
      scheduler skip the very row the operator just asked it to run.
    * ``top`` takes its priority from the caller instead of applying a fixed
      step.  A constant ties every row of a multi-row selection at one value,
      which hands the final ordering to whatever the database returns -- the
      opposite of "move these to the front".
    """
    if action == "retry":
        if item.status == "RUNNING":
            return "running", False
        item.status = "PENDING"
        item.attempt_count = 0
        # The budget is read from ``outcome_count``, so this is the counter that
        # actually matters: leaving it at the cap would make the scheduler skip
        # the very row the operator just asked it to run.
        item.outcome_count = 0
        item.last_error = None
        item.finished_at = None
        return None, False
    if action == "cancel":
        if item.status in QUEUE_FINISHED_STATUSES:
            return "already_finished", False
        item.status = "CANCELLED"
        item.finished_at = datetime.now(UTC)
        return None, False
    if action == "top":
        if item.status in QUEUE_FINISHED_STATUSES:
            return "already_finished", False
        # 1000 per move, against a default of 100, so a single-item move is
        # unambiguous.  A multi-row move passes an explicit rank per row instead:
        # see ``top_priority``.
        item.priority = (
            int(top_priority) if top_priority is not None else int(item.priority) - 1000
        )
        return None, False
    if item.status == "RUNNING":
        return "running", False
    return None, True


@router.post("/queue/bulk")
async def arena_queue_bulk(
    payload: ArenaQueueBulkRequest, session: AsyncSession = Depends(get_session)
) -> dict:
    """Apply one action to many queue rows.

    Exists because the interactive surface needs it: a backlog of 100 challenges
    is unusable if clearing a failed run means clicking each row.  Every action is
    per-row guarded and reported, so one ineligible row (cancelling something
    already solved) cannot abort the rest of the selection.
    """
    items = list(
        (
            await session.scalars(
                select(ArenaSolveQueueItem).where(ArenaSolveQueueItem.id.in_(payload.item_ids))
            )
        ).all()
    )
    found = {item.id for item in items}
    # Reported rather than ignored: an id that no longer exists usually means the
    # list the caller acted on is stale, and a silent partial success hides that.
    missing = [item_id for item_id in payload.item_ids if item_id not in found]

    # "Move to top" ranks the whole selection against the backlog, so the batch
    # keeps its internal order instead of every row landing on one value.  The
    # rank *ascends* in processing order because a lower priority runs sooner:
    # the first row of the selection must end up with the smallest number.
    rank: int | None = None
    if payload.action == "top":
        lowest = await session.scalar(
            select(func.min(ArenaSolveQueueItem.priority)).where(
                ArenaSolveQueueItem.status == "PENDING"
            )
        )
        rank = int(lowest or 0) - len(items)

    changed = 0
    skipped: list[dict] = []
    for item in items:
        reason, should_delete = _apply_queue_bulk_action(
            item, payload.action, top_priority=rank
        )
        if reason:
            skipped.append({"id": item.id, "reason": reason})
            continue
        if should_delete:
            await session.delete(item)
        if rank is not None:
            rank += 1
        changed += 1
    await session.commit()
    return {"data": {"action": payload.action, "changed": changed, "skipped": skipped, "missing": missing}}


@router.post("/queue/clear")
async def arena_queue_clear(
    status: str = "PENDING", session: AsyncSession = Depends(get_session)
) -> dict:
    """Delete queue rows in one status.  Defaults to PENDING (the backlog).

    Kept to one status per call on purpose: "clear everything" would also wipe the
    solved history that the scorebook cross-references.
    """
    items = list(
        (
            await session.scalars(
                select(ArenaSolveQueueItem).where(ArenaSolveQueueItem.status == status)
            )
        ).all()
    )
    for item in items:
        await session.delete(item)
    await session.commit()
    return {"data": {"deleted": len(items), "status": status}}
