"""Arena solve loop: fetch a challenge, run it locally against a leased target, submit.

The shape of a solve, and why it is this order
----------------------------------------------
The arena's own instructions describe the loop, and one constraint from the API
forces a specific ordering on everything else::

    pick challenge -> read it -> lease environment -> wait for access_ready
    -> attack -> submit -> destroy

A challenge cannot be attacked before its environment is up, and ``submit_flag``
is refused unless the target is still running -- challenges carry
``requires_running_target_for_submit``.  So this module does **not** create the
local challenge first and start a Run afterwards: it leases the environment
first, then writes ``target_url`` and ``allowed_hosts`` from the lease, then
creates the Run.  Getting that backwards produces a Run with nothing to point at.

Scope
-----
This is the **minimal closed loop for one challenge**, deliberately.  The platform
allows a single environment, so a queue would be mostly lease-management
complexity; proving one challenge end to end is what makes a queue worth writing.
Nothing here enumerates or auto-picks beyond what the caller names.

Notes are sanitised
-------------------
The platform requires ``ctf2_agent_log_note`` and separately forbids putting
flags in notes.  :func:`_sanitise_note` therefore strips anything shaped like a
flag before it leaves the process -- the requirement is on the write path, so a
filter on the read path would not satisfy it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import DomainError
from app.core.net import ensure_absolute_http_url
from app.models.arena import ArenaConnection
from app.models.challenge import Challenge
from app.models.model_config import ModelConfig
from app.models.run import RunEvent, SolveRun
from app.orchestration.state_machine import TERMINAL, parse_run_status
from app.schemas.challenge import normalize_flag_pattern
from app.services import arena_ledger
from app.services.arena_lease import arena_lease_manager
from app.services.arena_mcp import arena_mcp_clients
from app.services.crypto import decrypt_api_key
from app.services.events import event_service
from app.services.flags import flag_service
from app.services.run_lifecycle import cancel_run
logger = logging.getLogger(__name__)

#: Per-challenge wall clock for an arena solve.
#:
#: The platform serves one environment at a time, so this figure multiplied by
#: the batch size is how long the only slot stays busy.  An earlier 60-minute
#: budget made a 30-challenge batch a multi-day affair.
#:
#: Raised from 1200 to 1800 on 2026-10-25, after measuring what the old value
#: actually did.  Nine consecutive Easy Web runs all ended between 897s and
#: 1085s, against the old 1200s budget -- none of them ran out of *total* time.
#: Their worker phase, on the other hand, landed at **755-760s every single
#: time**, which is ``min(720, max_runtime_seconds)`` plus launch overhead.  The
#: Runs were not finishing, they were being cut off mid-investigation at the
#: twelve-minute mark, having spent 150-320 steps on reconnaissance and never
#: reaching the exploit request.  Every one of them then spent its remaining
#: budget on a single Intent and finalised with ``reason`` unset.
#:
#: So the old number was tuned to the wrong constraint.  1800s leaves room for a
#: 1500s worker phase (double the old one) plus the 120s convergence window and
#: finalisation.  It is deliberately not larger: if a practice box does not fall
#: in 25 minutes of solid work, the next lever is the method, not the clock --
#: and ``RUN_CONVERGENCE_SECONDS`` stays at 120 either way, because that window
#: is the safety valve that stops a drifting Run, not a difficulty knob.
ARENA_RUN_BUDGET_SECONDS = 1800

#: How long one Worker round may run before the Coordinator reaps it.
#:
#: Separate from the Run budget above because the two are used differently: the
#: budget bounds the whole solve, this bounds a single dispatch inside it.  The
#: old ``min(720, ...)`` cap was what actually curtailed every measured run, so
#: it is lifted here rather than scaled silently by the budget.
#:
#: Still capped relative to the Run budget (see the caller) so a Worker can never
#: outlive its own Run, and still below it so convergence and finalisation have
#: room to happen.
ARENA_WORKER_ROUND_SECONDS = 1500

#: How long to wait for a Run to reach a terminal state before giving up and
#: destroying the environment anyway.
#:
#: This must be **larger** than the Run's own budget (ARENA_RUN_BUDGET_SECONDS),
#: or the wait expires while the Run is still legitimately working and the
#: environment is destroyed underneath it -- turning a slow solve into a failed
#: one and leaving an orphaned Run.  The margin is deliberate slack for the Run's
#: own finalisation after it stops working.
RUN_TIMEOUT_SECONDS = ARENA_RUN_BUDGET_SECONDS + 300
RUN_POLL_INTERVAL_SECONDS = 10.0

#: How long a Run may go without a single Worker command before it is cancelled.
#:
#: ``RUN_TIMEOUT_SECONDS`` is the worst case for a Run that is *working*.  It is
#: the wrong bound for one that never started: when a Worker CLI cannot reach its
#: relay it opens a request, the request dies, the CLI reconnects, and nothing is
#: ever executed.  The Run then spends its full budget producing zero
#: ``muteki.worker_command`` events -- observed at 13 and 26 minutes on two
#: consecutive challenges, with nine dead SSE connections and not one file in the
#: container.
#:
#: Every Run that actually works emits its first command well inside a minute
#: (a solved challenge took two), so ten minutes without one is a wedge, not a
#: slow start.  Cancelling early is not a loss: the item goes back to the queue
#: with its environment released, instead of holding the platform's only slot for
#: the rest of the budget.
#:
#: Only *zero* commands are treated as wedged.  A Run that ran commands and then
#: went silent is a different diagnosis (the model is stuck rather than the
#: transport being down) and deliberately keeps its full budget.
RUN_STALL_GRACE_SECONDS = 600.0

#: The event that proves a Worker executed something.  Written by the engine, not
#: by the model, so its absence cannot be explained away by a chatty turn.
WORKER_PROGRESS_EVENT = "muteki.worker_command"

#: How many spellings of the same finding may be submitted for one Run.
#:
#: A value can now be offered under several banners, so an unbounded loop would
#: send one request per (value x banner) pair -- a Run that found four junk
#: ``{...}`` strings would spend twenty writes, and the platform rate-limits
#: (``RATE_LIMIT_EXCEEDED``) long before that.  Five covers the ordered set the
#: operator asked for: as-printed, then the declared banner, then the family.
MAX_SUBMISSIONS_PER_RUN = 5

#: The flag banner this platform uses.  Established from accepted submissions,
#: not assumed: every flag the arena has actually accepted was ``CTF2{<uuid>}``.
#: Kept as a default so a differently-branded arena is a config change, not a
#: code change.
DEFAULT_ARENA_FLAG_BANNER = "CTF2"

#: Sent when the platform reports the challenge as already solved. Distinct from
#: a rejection: nothing was attempted, so it must not count against the
#: rejection circuit breaker.
VERDICT_ALREADY_SOLVED = "already_solved"

# Anything shaped like a flag, for note sanitisation. Covers the platform's own
# banner plus the generic shape this codebase already recognises.
_FLAG_LIKE = re.compile(r"(?i)\b[a-z0-9_]{2,32}\{[^{}\r\n]{1,256}\}")


def _sanitise_note(text: str) -> str:
    """Remove flag-shaped strings before a note is written.

    Done on the write path because that is where the prohibition applies: a note
    that reached the platform with a flag in it cannot be unsent.
    """
    cleaned = _FLAG_LIKE.sub("[redacted:flag]", text or "")
    # The platform caps notes at 2000 characters (quotas.note_max_chars).
    return cleaned[:1900]


def _is_terminal(status: str) -> bool:
    """Whether a Run status ends the Run.

    Delegates to the state machine's ``TERMINAL`` set instead of a private
    prefix rule.  The prefix rule only saw ``COMPLETED*`` / ``FAILED*``, so it
    missed ``CANCELLED`` -- and cancelling a Run therefore left its arena item
    "in flight" until ``RUN_TIMEOUT_SECONDS`` expired: the queue kept showing a
    live-looking item for twenty-five minutes after the operator had already
    stopped it, the scheduler would not move on to the next challenge, and
    nothing anywhere said why.  ``TIMEOUT`` and ``POLICY_BLOCKED`` were missed
    the same way.

    Unknown values read as *not terminal*, matching the tolerant parser used
    everywhere else in the reconciliation paths: keep waiting rather than
    declare a Run finished on a status we cannot interpret.
    """
    parsed = parse_run_status(status)
    return parsed is not None and parsed in TERMINAL


class ArenaSolveOutcome:
    def __init__(self) -> None:
        self.challenge_id: str = ""
        self.local_challenge_id: str = ""
        self.run_id: str = ""
        self.run_status: str = ""
        #: Why the Run ended, when it ended without attempting the challenge.
        #:
        #: The queue uses this to tell "the box beat us" from "we never got to
        #: try".  ``NO_HEALTHY_WORKER_PROFILE`` closes a Run before a single
        #: Intent exists, and charging that to the challenge both retires the
        #: item permanently and inflates the unsolved count with boxes nothing
        #: ever probed.  Empty for an ordinary solve.
        self.run_status_reason: str = ""
        #: Whether the platform's single environment slot was needed at all.
        self.needs_environment: bool = False
        self.environment: dict = {}
        self.candidates: list[str] = []
        self.submissions: list[dict] = []
        self.verdict: str | None = None
        self.error: str | None = None
        #: Result of replaying the winning path in a clean Runner session.
        #:
        #: Recorded *after* the platform has already accepted the flag, and
        #: deliberately unable to change the verdict: the arena's answer is
        #: authoritative, and a reproduction that fails on an already-reclaimed
        #: target must not turn an accepted solve into a loss.  This exists
        #: because ``fresh_reproduction_verified`` was 0 for every Run in the
        #: database -- the arena path never called the reproducer at all, so
        #: there was no evidence any success was reproducible.
        self.fresh_reproduction: dict = {}
        self.notes: list[str] = []

    def describe(self) -> dict:
        return {
            "platform_challenge_id": self.challenge_id,
            "local_challenge_id": self.local_challenge_id,
            "run_id": self.run_id,
            "run_status": self.run_status,
            "needs_environment": self.needs_environment,
            "environment": self.environment,
            "flag_candidates": self.candidates,
            "submissions": self.submissions,
            "verdict": self.verdict,
            "error": self.error,
            "notes": self.notes,
        }


async def fetch_challenge(client, *, practice_ground_id: str, challenge_id: str) -> dict:
    """Read one challenge in full.  Returns the ``challenge`` object."""
    payload = await client.get_practice_challenge(
        practice_ground_id=practice_ground_id, challenge_id=challenge_id
    )
    challenge = payload.get("challenge") if isinstance(payload, dict) else None
    if not isinstance(challenge, dict) or not challenge.get("id"):
        raise DomainError(
            "ARENA_CHALLENGE_UNREADABLE",
            "The arena did not return a readable challenge.",
            {"payload": payload},
            502,
            stage="ARENA_SOLVE",
        )
    return challenge


async def upsert_local_challenge(
    session: AsyncSession,
    *,
    connection: ArenaConnection,
    practice_ground_id: str,
    detail: dict,
) -> Challenge:
    """Mirror an arena challenge into the local Challenge table.

    Reuses ``metadata_json`` rather than adding columns: the arena's fields are
    descriptive metadata, and the table already has a JSON column whose stated
    purpose is exactly that.  ``target_url``/``allowed_hosts`` are left empty here
    and filled from the lease -- the arena chooses the address per environment, so
    a URL recorded at fetch time would be stale before the Run starts.
    """
    platform_id = str(detail["id"])
    existing = await session.scalar(
        select(Challenge).where(
            Challenge.metadata_json["arena_challenge_id"].as_string() == platform_id,
            Challenge.metadata_json["arena_connection_id"].as_string() == connection.id,
        )
    )
    metadata = {
        **(existing.metadata_json if existing else {}),
        "adapter": "dasctf_arena",
        "arena_connection_id": connection.id,
        "arena_practice_ground_id": practice_ground_id,
        "arena_challenge_id": platform_id,
        "friendly_id": detail.get("friendly_id"),
        "category": detail.get("category"),
        "difficulty": detail.get("difficulty"),
        "points": detail.get("points"),
        "has_attachments": bool(detail.get("has_attachments")),
        "has_container": bool(detail.get("has_container")),
        "requires_running_target_for_submit": bool(detail.get("requires_running_target_for_submit")),
        "practice_ground_name": detail.get("practice_ground_name"),
        "solve_count": detail.get("solve_count"),
    }
    description = str(detail.get("description") or "")
    item = existing or Challenge(
        # The friendly id is what an operator recognises; the UUID is the key.
        name=str(detail.get("name") or detail.get("friendly_id") or platform_id)[:200],
        challenge_type="WEB_TARGET",
    )
    item.name = str(detail.get("name") or detail.get("friendly_id") or platform_id)[:200]
    item.description = description
    item.metadata_json = metadata
    # The flag banner belongs to the PLATFORM, not to the mirrored challenge.
    #
    # This platform issues ``CTF2{<uuid>}`` for every container instance,
    # whatever banner the original event used -- ``[34C3CTF 2017]urlstorage``,
    # ``BUU XXE COURSE 1`` and ``[ASIS 2019]Unicorn shop`` all had a
    # ``CTF2{...}`` value ACCEPTED by the platform.  The previous rule only
    # stamped a banner onto a row that had *none*, so rows seeded with the
    # generic ``flag{...}`` placeholder kept it forever.  That was survivable
    # only while nothing re-checked the declared pattern; once flag verification
    # started re-applying it, the stale placeholder would have rejected 13
    # flags the platform had already accepted -- turning real solves into
    # "unsolved" and blocking their submission.  The platform setting is
    # therefore re-applied on every import, and a genuinely per-challenge shape
    # is recorded explicitly in ``flag_pattern_override``.
    platform_pattern = normalize_flag_pattern(
        get_settings().arena_flag_banner or DEFAULT_ARENA_FLAG_BANNER
    )
    override = str((metadata or {}).get("flag_pattern_override") or "").strip()
    desired_pattern = override or platform_pattern
    if str(item.flag_pattern or "").strip() != desired_pattern:
        if str(item.flag_pattern or "").strip():
            logger.info(
                "arena.challenge.flag_pattern_refreshed",
                extra={
                    "arena_challenge_id": platform_id,
                    "previous": str(item.flag_pattern),
                    "current": desired_pattern,
                },
            )
        item.flag_pattern = desired_pattern
    if existing is None:
        session.add(item)
    await session.commit()
    await session.refresh(item)
    return item


async def _load_candidates(session: AsyncSession, run_id: str) -> list[str]:
    """Read the candidates that may be submitted, best first.

    Uses the persisted candidates rather than re-scanning tool output: extraction
    already happened with the challenge's own pattern, and re-deriving it here
    would be a second, weaker implementation of the same thing.

    **Every** candidate is returned, including one whose banner does not match
    the challenge's declaration.  That is deliberate, and it is the whole point
    of the candidate mechanism: the arena mints a per-instance flag, so the value
    a box prints is sometimes the answer while its wrapper is the box's own
    (``n1book{afr_1_solved}`` on the N1BOOK targets).  Holding those back meant
    the real answer was recorded and then never tried -- run ``de9ab053`` had
    ``n1book{afr_1_solved}`` sitting in ``flag_candidates`` while only the
    host-invented ``CTF2{afr_1_solved}`` was ever submitted.

    Each value is expanded into several spellings, tried in this order:

    1. **the value exactly as the box printed it**, banner included.  Measured
       twice on the real-class boxes: ``n1book{afr_1_solved}`` was accepted while
       ``CTF2{afr_1_solved}`` was rejected one second earlier, and ``flag{
       r0bots_1s_s0_us3ful...}`` was accepted after ``CTF2{...}`` was rejected.
       The box's own wrapper is the strongest evidence of what the arena wants.
    2. **the declared banner** around the bare value -- the platform's own
       spelling, which wins whenever the box printed a hint rather than the flag.
    3. **the common banners** (``flag``, ``n1book``, ``CTF2``, ``hitcon``).  A
       challenge whose ``flag_pattern`` was registered under the wrong banner
       still gets a chance, and the nightly runs produced real examples of each.

    Ordering within a candidate is fixed by the caller-facing contract above:
    a human ``VALID`` promotion and a pattern match both rank higher, so a
    reviewed value is never buried under a guessed wrapper.
    """

    from app.models.challenge import Challenge
    from app.models.run import FlagCandidate, SolveRun

    items = list(
        (await session.scalars(select(FlagCandidate).where(FlagCandidate.run_id == run_id))).all()
    )
    def rank(item: FlagCandidate) -> tuple:
        return (
            0 if item.review_state == "VALID" else 1,
            0 if item.pattern_matched else 1,
            item.created_at or datetime.now(UTC),
        )

    declared = await session.scalar(
        select(Challenge.flag_pattern)
        .join(SolveRun, SolveRun.challenge_id == Challenge.id)
        .where(SolveRun.id == run_id)
    )
    from app.solver.muteki.recovery import _WRAPPED_RE, banner_of

    wanted = banner_of(declared)
    #: Banners worth trying besides the declared one.  ``flag`` is the CTF default
    #: and is what several of these boxes print; ``n1book`` and ``hitcon`` were
    #: the measured winners on the real-class and HITCON boxes.  ``CTF2`` is the
    #: arena's own mint and stays in the list even when it is already ``wanted``
    #: so the ordering below is stable.
    family = tuple(dict.fromkeys([wanted, "flag", "n1book", "CTF2", "hitcon"]))

    seen: list[str] = []

    def add(spelling: str) -> None:
        if spelling and spelling not in seen:
            seen.append(spelling)

    for item in sorted(items, key=rank):
        if not item.candidate:
            continue
        # A human's rejection is final.
        if item.review_state == "INVALID":
            continue
        raw = item.candidate
        # 1. exactly as the box printed it.
        add(raw)
        # 2. the bare value under each banner, declared banner first.
        inner = raw
        match = _WRAPPED_RE.fullmatch(raw.strip())
        if match:
            inner = match.group(2)
        elif "{" in raw or "}" in raw:
            # Malformed wrapper: it cannot be submitted as-is and its inner value
            # is not trustworthy enough to re-wrap, so skip the re-spellings.
            continue
        for banner in family:
            add(f"{banner}{{{inner}}}")
    return seen


async def _count_held_candidates(session: AsyncSession, run_id: str) -> int:
    """Count candidates that are waiting for a human before any submission."""

    from app.models.run import FlagCandidate

    # ``execute``, not ``scalars``: ``scalars`` keeps only the first column of
    # each row, so this three-column select would hand back bare strings and
    # the unpacking below would fail with "too many values to unpack (expected
    # 3)" -- but only once a candidate row exists, i.e. exactly on the runs that
    # solved the challenge.  A crash at that point skips submission entirely,
    # and the environment is destroyed before anyone can retry.
    rows = await session.execute(
        select(FlagCandidate.candidate, FlagCandidate.review_state, FlagCandidate.pattern_matched)
        .where(FlagCandidate.run_id == run_id)
    )
    return sum(
        1
        for candidate, review_state, pattern_matched in rows.all()
        if candidate and review_state == "OPEN" and not pattern_matched
    )


async def _dead_end_reason(session: AsyncSession, run_id: str) -> str:
    """The Coordinator's own stop reason, read from the event log.

    The Supervisor does not copy the Coordinator's stop reason onto the Run row,
    so a Run closed by ``NO_HEALTHY_WORKER_PROFILE`` arrives here with an empty
    ``last_error_code``.  The reason exists only as a ``muteki.dead_end``
    payload -- and only the *first* one matters, because the Coordinator emits a
    second dead end while finalising (``ROUND_BUDGET_EXHAUSTED_BEFORE_REASON``
    or a bare ``WORKER_TIMEOUT``) that describes the teardown, not the cause.
    """

    payload = await session.scalar(
        select(RunEvent.payload_json)
        .where(RunEvent.run_id == run_id, RunEvent.event_type == "muteki.dead_end")
        .order_by(RunEvent.sequence.asc())
        .limit(1)
    )
    if not payload:
        return ""
    try:
        body = json.loads(payload) if isinstance(payload, str) else payload
    except ValueError:
        return ""
    inner = body.get("payload") if isinstance(body, dict) else None
    if isinstance(inner, dict):
        inner = inner.get("payload") if isinstance(inner.get("payload"), dict) else inner
    if not isinstance(inner, dict):
        return ""
    return str(inner.get("reason") or inner.get("description") or "")


async def wait_for_run(
    session: AsyncSession, run_id: str, *, timeout: float = RUN_TIMEOUT_SECONDS
) -> str:
    """Poll until the Run is terminal, then return its status string.

    Also enforces the *no-progress* bound: a Run that has executed nothing by
    ``RUN_STALL_GRACE_SECONDS`` is cancelled rather than left to burn its whole
    budget (see that constant for what the failure looks like).  The check runs
    once -- after it finds any command at all, the Run is working and only its
    own budget applies.
    """
    waited = 0.0
    stall_checked = False
    while waited < timeout:
        await asyncio.sleep(RUN_POLL_INTERVAL_SECONDS)
        waited += RUN_POLL_INTERVAL_SECONDS
        status = await session.scalar(
            select(SolveRun.status).where(SolveRun.id == run_id)
        )
        await session.commit()  # drop the snapshot so the next read sees new rows
        if status and _is_terminal(str(status)):
            return str(status)
        if not stall_checked and waited >= RUN_STALL_GRACE_SECONDS:
            stall_checked = True
            executed = await session.scalar(
                select(func.count())
                .select_from(RunEvent)
                .where(
                    RunEvent.run_id == run_id,
                    RunEvent.event_type == WORKER_PROGRESS_EVENT,
                )
            )
            await session.commit()
            if not executed:
                if await _cancel_stalled_run(session, run_id):
                    return "STALLED_NO_WORKER_COMMAND"
    return "TIMEOUT_WAITING"


async def _cancel_stalled_run(session: AsyncSession, run_id: str) -> bool:
    """Close a Run whose Workers never executed anything.  True if it closed.

    A failure here is reported and *not* turned into a status: the Run may still
    be live, and ``wait_for_run`` returning "stalled" for a running Run would
    make the caller release the environment underneath it.
    """
    reason = (
        f"No Worker command within {int(RUN_STALL_GRACE_SECONDS)}s; the Run was "
        "cancelled so the batch can move on instead of spending the rest of the "
        "budget on a dead engine."
    )
    try:
        await cancel_run(session, run_id, reason)
    except Exception as error:  # noqa: BLE001 - a failed cancel must not crash the wait
        logger.warning("Could not cancel stalled run %s: %s", run_id, error)
        return False
    # The row is what the caller classifies on ("engine never ran" must not
    # retire the item), and ``cancel_run`` does not set a code of its own.
    try:
        run = await session.get(SolveRun, run_id, populate_existing=True)
        if run is not None:
            run.last_error_code = "WORKER_STALL_NO_COMMANDS"
            await session.commit()
    except Exception:  # noqa: BLE001 - classification is a hint, not the outcome
        logger.warning("Could not stamp the stall code on run %s", run_id, exc_info=True)
    return True


async def start_run_for_arena(
    session: AsyncSession,
    *,
    challenge: Challenge,
    model_config_id: str,
    engine_type: str,
    solver_mode: str,
    worker_engines: list[dict] | None = None,
    reason_model_config_id: str | None = None,
    max_runtime_seconds: int = ARENA_RUN_BUDGET_SECONDS,
    max_total_runtime_seconds: int = ARENA_RUN_BUDGET_SECONDS,
    knowledge_mode: str | None = None,
) -> SolveRun:
    """Create **and start** a Run for an arena challenge.

    Lives here rather than in the route so both the single-solve endpoint and the
    queue scheduler can use it without importing ``api/v1/runs`` from a service
    (which would be a cycle).

    Deliberately delegates to the ``create_run`` route the UI uses instead of
    duplicating Run construction: that function carries skill snapshotting,
    workspace creation, solver-state initialisation and prediction wiring, and a
    second implementation would drift from it silently.

    ``worker_engines`` / ``reason_model_config_id`` carry Muteki's multi-engine
    split.  They are optional and default to empty, so a connection configured for
    a single model still produces exactly the Run it produced before.
    """
    from app.api.v1.runs import RunCreate, create_run, start_run
    from app.schemas.run import WorkerEngineSelection

    payload = RunCreate(
        engine_type=engine_type,
        solver_mode=solver_mode,
        model_config_id=model_config_id,
        worker_engines=[
            WorkerEngineSelection.model_validate(entry) for entry in (worker_engines or [])
        ],
        reason_model_config_id=reason_model_config_id,
        # Arena challenges are container-backed and need live interaction, so a
        # short budget would fail before the target is usefully explored.
        max_agent_steps=200,
        max_tool_calls=200,
        max_runtime_seconds=max_runtime_seconds,
        max_total_runtime_seconds=max_total_runtime_seconds,
        knowledge_mode=knowledge_mode,
    )
    created = await create_run(challenge.id, payload, session)
    # ``create_run`` returns ``{"data": <RunRead>}`` -- a model, not a mapping, so
    # the id is an attribute rather than a key. Reading it as ``["data"]["id"]``
    # was a real bug, caught by actually running the loop.
    run_id = created["data"].id
    await start_run(run_id, session)
    run = await session.get(SolveRun, run_id)
    if run is None:
        raise DomainError(
            "ARENA_RUN_NOT_CREATED",
            "The solve run could not be created.",
            status_code=500,
            stage="ARENA_SOLVE",
        )
    return run


async def solve_one(
    session: AsyncSession,
    *,
    connection: ArenaConnection,
    practice_ground_id: str,
    challenge_id: str,
    start_run_fn,
    on_run_created=None,
) -> ArenaSolveOutcome:
    """The minimal closed loop for a single challenge.

    ``start_run_fn`` is injected rather than imported: the route lives in
    ``api/v1/runs`` and importing it here would create a cycle. It is the same
    function the HTTP endpoint calls, so behaviour cannot drift.

    ``on_run_created`` is called with the Run id the moment it exists, before the
    solve starts waiting on it.  The batch queue uses it to record the id on its
    row immediately: the row is only written back after the solve returns, so
    without this a solve that is legitimately running for 20 minutes looks
    identical to one that never created a Run at all -- and an operator watching
    the queue has no way to open the Run and see the work happening.

    **The environment is leased only when the challenge needs one.** Measured on
    the live arena, a large share of the catalogue is attachment-only
    (``has_container: false`` -- 73 of DASBOOK's 92 challenges), and those solve
    entirely offline. Leasing for them would occupy the platform's single slot for
    nothing and block a container challenge that genuinely needs it.

    When a lease *is* taken it precedes the local challenge write, because the
    challenge's ``target_url`` comes from the lease. Everything after the lease
    runs inside the ``async with``, so a failure at any point still destroys the
    environment.
    """
    outcome = ArenaSolveOutcome()
    outcome.challenge_id = challenge_id
    token = decrypt_api_key(connection.encrypted_token) if connection.encrypted_token else None
    client = await arena_mcp_clients.get(connection, token)

    detail = await fetch_challenge(
        client, practice_ground_id=practice_ground_id, challenge_id=challenge_id
    )
    outcome.needs_environment = bool(detail.get("has_container"))

    # The platform reports whether this agent already solved the challenge. Stop
    # here rather than solving again: a re-solve burns the single environment slot
    # for a result that cannot improve, and a duplicate submission is wasted quota.
    if detail.get("is_solved"):
        outcome.verdict = VERDICT_ALREADY_SOLVED
        outcome.run_status = "SKIPPED_ALREADY_SOLVED"
        await _note(client, challenge_id, "plan", "Challenge already solved by this agent; skipping.")
        return outcome

    if not outcome.needs_environment:
        await _note(
            client,
            challenge_id,
            "plan",
            "Attachment-only challenge; no environment required.",
        )
        local = await upsert_local_challenge(
            session,
            connection=connection,
            practice_ground_id=practice_ground_id,
            detail=detail,
        )
        outcome.local_challenge_id = local.id
        await _run_and_submit(
            session,
            client=client,
            connection=connection,
            practice_ground_id=practice_ground_id,
            challenge_id=challenge_id,
            detail=detail,
            local=local,
            outcome=outcome,
            start_run_fn=start_run_fn,
            lease_manager=None,
            lease=None,
            on_run_created=on_run_created,
        )
        return outcome

    async with arena_lease_manager as lease_manager:
        lease = await lease_manager.acquire(
            client,
            connection_id=connection.id,
            practice_ground_id=practice_ground_id,
            challenge_id=challenge_id,
        )
        outcome.environment = lease.describe()
        await _note(client, challenge_id, "plan", f"Environment ready at {lease.access_url}")

        local = await upsert_local_challenge(
            session,
            connection=connection,
            practice_ground_id=practice_ground_id,
            detail=detail,
        )
        outcome.local_challenge_id = local.id
        # The address is only known now; record it before the Run reads it.
        # Some grounds publish a bare ``host:port``; ``target_url`` is an
        # absolute-URL contract, and storing the bare form made every later
        # read of this challenge fail validation -- which surfaced as the whole
        # challenge list returning HTTP 500, not as one bad row.
        if lease.access_url:
            local.target_url = ensure_absolute_http_url(lease.access_url)
            local.allowed_hosts = list(lease.allowed_hosts)
            await session.commit()

        await _run_and_submit(
            session,
            client=client,
            connection=connection,
            practice_ground_id=practice_ground_id,
            challenge_id=challenge_id,
            detail=detail,
            local=local,
            outcome=outcome,
            start_run_fn=start_run_fn,
            lease_manager=lease_manager,
            lease=lease,
            on_run_created=on_run_created,
        )
    return outcome


async def _run_and_submit(
    session: AsyncSession,
    *,
    client,
    connection: ArenaConnection,
    practice_ground_id: str,
    challenge_id: str,
    detail: dict,
    local: Challenge,
    outcome: ArenaSolveOutcome,
    start_run_fn,
    lease_manager,
    lease,
    on_run_created=None,
) -> None:
    """Start the Run, wait for it, then submit whatever it extracted.

    Shared by both paths: an attachment-only challenge and a container challenge
    differ in whether a lease is held, not in how the solve is run or how a result
    is submitted.
    """
    # Fall back to the global default from system settings when the connection
    # carries no model of its own: the per-connection value is the more specific
    # choice, but a global default exists precisely so every connection does not
    # have to repeat the same pick.
    model_config_id = connection.default_model_config_id or get_settings().default_model_config_id
    if not model_config_id:
        raise DomainError(
            "ARENA_DEFAULT_MODEL_MISSING",
            "No default model is available for this solve; set one on the arena connection "
            "or configure a global default in system settings.",
            status_code=422,
            stage="ARENA_SOLVE",
        )
    config = await session.get(ModelConfig, model_config_id)
    if not config or not config.enabled:
        raise DomainError(
            "ARENA_DEFAULT_MODEL_DISABLED",
            "The connection's default model configuration is missing or disabled.",
            status_code=422,
            stage="ARENA_SOLVE",
        )

    run = await start_run_fn(
        session,
        challenge=local,
        model_config_id=model_config_id,
        engine_type=connection.default_engine_type or config.provider_type,
        solver_mode=connection.default_solver_mode or "muteki",
        # Multi-engine selection is passed through untouched; ``create_run`` is
        # where it is validated, so a bad entry fails with the same error a manual
        # Run would give rather than a second, divergent one here.
        worker_engines=list(connection.default_worker_engines or []),
        reason_model_config_id=connection.default_reason_model_config_id,
        # Stated explicitly rather than left to the deployment default: the batch
        # path is where the corpus is most valuable (many challenges, no human in
        # the loop to seed context), and it is the path that never used to say
        # anything at all.
        knowledge_mode=get_settings().arena_knowledge_mode,
    )
    outcome.run_id = run.id
    # Published before the wait, not after: the caller (the batch queue) writes
    # its row back only when this function returns, so a Run id reported at the
    # end would leave the row saying "no Run" for the entire solve -- the exact
    # window an operator wants to look at it in.
    if on_run_created is not None:
        try:
            await on_run_created(run.id)
        except Exception:  # noqa: BLE001 - bookkeeping must not fail the solve
            pass
    run_status = await wait_for_run(session, run.id)
    outcome.run_status = run_status
    # Why it ended.  The queue needs this to separate "the box beat us" from
    # "no engine was usable, we never tried" -- the latter must not retire the
    # item.  Read from the terminal Run row, not from the graph: a Run that dies
    # in PREPARE never writes a coordinator directive, and its reason is only on
    # the row the supervisor closed.
    #
    # ``last_error_code`` is **not** where this usually lives.  Measured
    # 2026-11-05: of 75 Runs that closed on ``NO_HEALTHY_WORKER_PROFILE``, 74 had
    # ``last_error_code IS NULL`` and the 75th said
    # ``MUTEKI_NATIVE_FINALIZED_RECONCILED_UNSOLVED``.  The reason only ever
    # reached the event log, as a ``muteki.dead_end`` payload.  So the queue's
    # ``_is_infrastructure_no_attempt`` guard -- which exists precisely to avoid
    # retiring an item over an upstream outage -- read an empty string every
    # time and never fired, and those 75 Runs were all filed as unsolved.
    try:
        terminal_row = await session.get(SolveRun, run.id, populate_existing=True)
        if terminal_row is not None:
            outcome.run_status_reason = str(terminal_row.last_error_code or "")
    except Exception:  # noqa: BLE001 - a classification hint must not fail the solve
        outcome.run_status_reason = ""
    if not outcome.run_status_reason:
        outcome.run_status_reason = await _dead_end_reason(session, run.id)

    # Submission needs a live target, and the solve may have consumed most of the
    # environment's window, so refresh it before acting on the result.
    if lease_manager is not None and lease is not None:
        await lease_manager.keep_alive(client, lease)

    # A native Muteki Run records its flag in ``report_json`` and materializes the
    # candidate lazily, on the read paths that call ``reconcile_run_status``.
    # Nothing in the arena loop did, so a solved Run left an empty candidate table
    # and there was nothing to submit. Reconciling here -- before reading -- is
    # what turns "the solver says solved" into "there is a flag to try".
    # Idempotent: when the candidate already exists it is one indexed lookup.
    run_row = await session.get(SolveRun, run.id)
    if run_row is not None:
        # Reload first.  The session is built with ``expire_on_commit=False``, and
        # ``wait_for_run`` polls the status through a scalar query that never
        # touches this object, so what ``get`` returns is still the row
        # ``start_run_for_arena`` inserted -- with an empty ``report_json``.
        # Reconciling against that finds no flag, leaves the candidate table
        # empty, and the loop then has nothing to submit: the Run reports
        # ``COMPLETED_SOLVED`` while the challenge stays unsolved on the platform.
        await session.refresh(run_row)
        await flag_service.reconcile_run_status(session, run_row)

    candidates = await _load_candidates(session, run.id)
    outcome.candidates = candidates
    if candidates:
        await _note(
            client,
            challenge_id,
            "finding",
            f"Run reached {run_status}; {len(candidates)} candidate(s) extracted.",
        )
    held = await _count_held_candidates(session, run.id)
    if held:
        # Leave a trace on the platform: a Run that ends with nothing submitted
        # otherwise looks like it simply found nothing, when in fact it found
        # something a human has to judge first.
        await _note(
            client,
            challenge_id,
            "finding",
            f"{held} suspected flag(s) did not match the challenge banner and are "
            "awaiting human review; they were NOT submitted.",
        )

    name = str(detail.get("name") or "")
    category = str(detail.get("category") or "")
    for candidate in candidates[:MAX_SUBMISSIONS_PER_RUN]:
        decision = await arena_ledger.evaluate_submission(
            session, connection_id=connection.id, challenge_id=challenge_id, flag=candidate
        )
        if not decision.allowed:
            await _record(
                session,
                connection=connection,
                challenge_id=challenge_id,
                candidate=candidate,
                verdict=decision.verdict,
                name=name,
                category=category,
                model_config_id=model_config_id,
            )
            outcome.submissions.append({"flag": candidate, "verdict": decision.verdict})
            continue
        try:
            result = await client.submit_flag(
                practice_ground_id=practice_ground_id,
                challenge_id=challenge_id,
                flag=candidate,
            )
        except Exception as error:  # noqa: BLE001 - a rejected flag is data, not a crash
            await _record(
                session,
                connection=connection,
                challenge_id=challenge_id,
                candidate=candidate,
                verdict=arena_ledger.VERDICT_REJECTED,
                name=name,
                category=category,
                model_config_id=model_config_id,
            )
            outcome.submissions.append(
                {"flag": candidate, "verdict": arena_ledger.VERDICT_REJECTED, "message": str(error)[:200]}
            )
            continue
        accepted = _submission_accepted(result)
        verdict = arena_ledger.VERDICT_ACCEPTED if accepted else arena_ledger.VERDICT_REJECTED
        await _record(
            session,
            connection=connection,
            challenge_id=challenge_id,
            candidate=candidate,
            verdict=verdict,
            name=name,
            category=category,
            model_config_id=model_config_id,
        )
        outcome.submissions.append({"flag": candidate, "verdict": verdict, "result": result})
        if accepted:
            outcome.verdict = verdict
            await _note(client, challenge_id, "result", "Flag accepted by the arena.")
            # Replay the winning path in a clean session while the target is
            # still leased.  This is the only window in which it is possible:
            # ``async with arena_lease_manager`` in the caller destroys the
            # environment as soon as this function returns, and the addresses
            # are single-use (verified: two historical targets both 404 while
            # the direct host still answers 200), so a solve that was not
            # reproduced here can never be reproduced later.
            outcome.fresh_reproduction = await _verify_reproduction(
                session,
                run_id=outcome.run_id,
                lease_manager=lease_manager,
                lease=lease,
                client=client,
            )
            await _publish_reproduction(session, outcome.run_id, outcome.fresh_reproduction)
            break

    if outcome.verdict is None:
        outcome.verdict = arena_ledger.VERDICT_REJECTED if candidates else "no_candidate"
        await _note(
            client,
            challenge_id,
            "result",
            f"No accepted flag. Run state {run_status}; {len(candidates)} candidate(s) tried.",
        )


#: Hard ceiling on the reproduction replay.  It runs while the platform's only
#: environment slot is still held, so an unbounded replay would block every
#: later challenge -- the same failure mode as a leaked lease.  A solve that
#: cannot be replayed in this window is recorded as unverified, not retried.
REPRODUCTION_BUDGET_SECONDS = 180


async def _verify_reproduction(
    session: AsyncSession,
    *,
    run_id: str,
    lease_manager,
    lease,
    client,
) -> dict:
    """Replay the winning path in a clean session; never change the verdict.

    Called only after the platform accepted the flag.  Every failure mode here
    is recorded and swallowed: the arena's acceptance is authoritative, and a
    reproduction problem is evidence about our own tooling, not a reason to
    downgrade a real solve.  The return value is what the queue and the dataset
    read, so a silent failure would be worse than a loud one -- the ``status``
    field always says which case happened.
    """

    if not run_id:
        return {"status": "skipped", "reason": "no_run_id", "verified": False}

    try:
        # The solve may have consumed most of the environment's window.
        if lease_manager is not None and lease is not None:
            await lease_manager.keep_alive(client, lease)
    except Exception as error:  # noqa: BLE001 - lease refresh is best effort
        logger.info("reproduction: keep_alive failed (%s)", type(error).__name__)

    async def replay() -> dict:
        from app.services.fresh_reproduction import fresh_reproduction_executor
        from app.services.reports import ReproductionPlanner

        run = await session.get(SolveRun, run_id, populate_existing=True)
        if run is None:
            return {"status": "skipped", "reason": "run_not_found", "verified": False}
        challenge = await session.get(Challenge, run.challenge_id)
        if challenge is None:
            return {"status": "skipped", "reason": "challenge_not_found", "verified": False}
        steps = await ReproductionPlanner().plan(session, run, challenge)
        validation = await fresh_reproduction_executor.execute(session, run, challenge, steps)
        return {
            "status": "executed",
            "verified": bool(validation.get("verified")),
            "blocked": validation.get("blocked") or "",
            "executed": bool(validation.get("executed")),
        }

    try:
        result = await asyncio.wait_for(replay(), timeout=REPRODUCTION_BUDGET_SECONDS)
    except asyncio.TimeoutError:
        return {"status": "timeout", "reason": f">{REPRODUCTION_BUDGET_SECONDS}s", "verified": False}
    except Exception as error:  # noqa: BLE001 - never fail a solve over this
        logger.warning("reproduction: replay raised %s: %s", type(error).__name__, error)
        return {"status": "error", "reason": f"{type(error).__name__}", "verified": False}
    return result


async def _publish_reproduction(session: AsyncSession, run_id: str, result: dict) -> None:
    """Record the replay outcome as a RunEvent.

    An event rather than a new column: the queue table has no field for this,
    and the dataset exporter already reads ``run_events``.  The event is written
    even when the replay was skipped or timed out -- "we did not verify" is a
    fact the training data must be able to see, otherwise an unverified solve is
    indistinguishable from a verified one at export time.
    """

    if not run_id or not result:
        return
    try:
        await event_service.append(
            session,
            run_id,
            "arena.fresh_reproduction",
            {
                "status": str(result.get("status") or ""),
                "verified": bool(result.get("verified")),
                "reason": str(result.get("reason") or result.get("blocked") or ""),
                "budget_seconds": REPRODUCTION_BUDGET_SECONDS,
            },
        )
        await session.commit()
    except Exception as error:  # noqa: BLE001 - bookkeeping must not fail the solve
        logger.info("reproduction: could not publish event (%s)", type(error).__name__)


async def _record(
    session: AsyncSession,
    *,
    connection: ArenaConnection,
    challenge_id: str,
    candidate: str,
    verdict: str,
    name: str,
    category: str,
    model_config_id: str,
) -> None:
    """Append one scorebook row.  Attribution is why this exists."""
    await arena_ledger.record_submission(
        session,
        connection_id=connection.id,
        challenge_id=challenge_id,
        flag=candidate,
        verdict=verdict,
        challenge_name=name,
        category=category,
        model_config_id=model_config_id,
        engine_key=connection.default_engine_type,
    )


async def _note(client, challenge_id: str, kind: str, text: str) -> None:
    """Write a sanitised note.  Never allowed to fail the solve."""
    try:
        await client.log_note(challenge_id=challenge_id, content=_sanitise_note(text), kind=kind)
    except Exception:  # noqa: BLE001 - the platform requires notes, but a note failure is not a solve failure
        pass


def _submission_accepted(result: object) -> bool:
    """Read the platform's verdict without assuming a specific field name.

    The submit response shape was not pinned down by probing (submitting a flag
    to a live challenge is a scoring action, not a read), so this accepts any of
    the plausible indicators rather than requiring one exact key -- and treats the
    absence of all of them as "not accepted", which is the safe direction.
    """
    if not isinstance(result, dict):
        return False
    for key in ("accepted", "correct", "is_correct", "success", "solved"):
        value = result.get(key)
        if isinstance(value, bool):
            return value
    status = str(result.get("status") or result.get("result") or "").lower()
    return status in {"accepted", "correct", "solved", "ok"}
