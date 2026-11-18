"""Serial batch scheduler for arena challenges.

The constraint that shapes everything
-------------------------------------
The platform serves **one environment at a time** per agent.  A batch of
container challenges therefore takes hours, and the scheduler's whole job is to
walk the backlog one item at a time without ever holding two environments or
losing its place.

Note that the backend's Run supervisor is already serial -- ``_worker_loop``
awaits each Run to completion before pulling the next wake-up -- so Runs do not
compete with each other.  What the queue adds is *ordering and durability*: which
challenge to attempt next, and what already happened when the process restarts.

Durability over elegance
------------------------
Two failure modes drove the design:

* **A crash mid-item.**  An item left in ``RUNNING`` with the process gone would
  never be retried, and would look like a stuck queue.  :meth:`recover` resets
  those to ``PENDING`` at startup, which is safe because an interrupted solve
  produced no accepted flag.
* **A restart re-solving a solved challenge.**  The platform reports
  ``is_solved`` per challenge, and :func:`arena_solve.solve_one` short-circuits on
  it.  That is the backstop; the queue's own ``SOLVED`` status is the fast path.

Ordering puts offline challenges first
--------------------------------------
Attachment-only challenges (``has_container: false``) need no environment at all.
Running them before container challenges keeps the single slot free for as long as
possible, and a large share of the catalogue is attachment-only, so this is a real
saving rather than a micro-optimisation.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import SessionLocal
from app.models.arena import ArenaConnection, ArenaSolveQueueItem
from app.models.challenge import Challenge
from app.models.run import SolveRun
from app.services import arena_lease, arena_solve
from app.services.arena_lease import arena_lease_manager

logger = logging.getLogger(__name__)

#: Statuses an item can hold.  ``RUNNING`` is the only one that means "in flight"
#: and is therefore the only one :meth:`recover` has to repair.
PENDING = "PENDING"
RUNNING = "RUNNING"
SOLVED = "SOLVED"
UNSOLVED = "UNSOLVED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"

#: Give up on an item after this many attempts.  A container challenge that fails
#: twice is more likely to be broken than unlucky, and each attempt costs an hour
#: of the single environment slot.
MAX_ATTEMPTS = 2

#: Pause between items.  Not a rate limit -- it keeps the loop from spinning hot
#: when the queue drains, and gives an operator a window to cancel.
IDLE_SLEEP_SECONDS = 5.0

#: Priority deltas applied when ordering pending items by their own history.
#:
#: The queue's ``priority`` column encodes only the *operator's* intent (and, at
#: enqueue time, whether an environment is needed).  It says nothing about
#: whether the box has ever been solvable, and measured 2026-11-06 the
#: difference is large: of the 133 ACTIVE challenges, **119 had been run before
#: and never solved**, while only **14 had ever produced a flag**.
#:
#: A run on the first group is close to a guaranteed loss -- CVE-2012-1823 in
#: particular had 5 runs and 0 solves, and the prompt already recorded *why*
#: (RCE works, the flag is not retrievable from that account).  Spending the
#: single environment slot there while a previously-solved box waits is the
#: most expensive ordering mistake available.
#:
#: Deltas rather than absolute values: an operator who explicitly moves an item
#: to the top must still win.  ``-200`` outranks the default 100 by two full
#: "move to top" steps, so ordinary enqueue order cannot accidentally invert it.
HISTORY_SOLVED_BEFORE = -200
HISTORY_NEVER_RUN = -100
#: Run once or twice without a flag -- not yet evidence of anything.  A single
#: failed Run is usually an engine hiccup, a bad environment lease or a timeout,
#: and measured 2026-11-06 a CVE box on this platform solves at **24%**, the same
#: as any other box (25%).  So one failure carries almost no information, while
#: demoting on it is permanent: the box drops behind every unexplored box and is
#: never picked again.  That is a self-fulfilling dead end -- the 42 imported-
#: payload boxes were ALL sitting in it, which is why none of them could ever be
#: verified.
#:
#: Sits *above* ``HISTORY_NEVER_RUN`` on purpose.  A box that has been touched
#: already has a workspace, a fingerprint and often a payload that was sent; it
#: costs less to resume than an untouched box costs to start.  It is also the
#: only group where a recipe can be checked against something real.
HISTORY_FEW_FAILURES = -150
#: Tried enough times that "it does not work here" is the better hypothesis.
#: Placed behind unexplored boxes but ahead of nothing: it still runs, just last.
HISTORY_NEVER_SOLVED = 300

#: Below this many attempts, a box is still "promising" rather than "measured".
PROMISING_RUN_LIMIT = 2


async def _order_by_history(
    session: AsyncSession, rows: list[ArenaSolveQueueItem]
) -> list[ArenaSolveQueueItem]:
    """Reorder pending items: previously solved first, run-but-unsolved last.

    Three buckets, and the middle one matters as much as the first:

    * **solved before** -- this box has produced a flag on this platform.  A
      re-run is the cheapest way to get a measured sample, which is exactly
      what ``verified_solve`` needs.
    * **never run** -- unknown, worth the slot; it neither gains nor loses rank.
    * **run, never solved** -- one measurement already says it does not work
      here.  Two attempts is the existing retry budget, so most of these have
      exhausted it; re-running them spends the slot to reconfirm a negative.

    Reads history only.  Nothing is written back, so a bad heuristic here can
    misorder a batch but can never corrupt the record of what happened.
    """

    platform_ids = {row.platform_challenge_id for row in rows if row.platform_challenge_id}
    if not platform_ids:
        return rows

    # local challenge id + arena id, for the same reason as ``_flagless_platform_ids``:
    # the marker lives in JSON, so the join is done in Python.
    challenges = list(
        (
            await session.scalars(
                select(Challenge).where(Challenge.status != "ARCHIVED")
            )
        ).all()
    )
    local_by_platform: dict[str, str] = {}
    for challenge in challenges:
        meta = challenge.metadata_json if isinstance(challenge.metadata_json, dict) else {}
        arena_id = str(meta.get("arena_challenge_id") or "")
        if arena_id:
            local_by_platform[arena_id] = str(challenge.id)

    if not local_by_platform:
        return rows

    local_ids = {
        local_by_platform[pid] for pid in platform_ids if pid in local_by_platform
    }
    if not local_ids:
        return rows

    stats = (
        await session.execute(
            select(
                SolveRun.challenge_id,
                func.count(SolveRun.id).label("runs"),
                func.sum(
                    case((SolveRun.status == "COMPLETED_SOLVED", 1), else_=0)
                ).label("solved"),
            )
            .where(SolveRun.challenge_id.in_(local_ids))
            .group_by(SolveRun.challenge_id)
        )
    ).all()
    history = {
        str(row.challenge_id): (int(row.runs or 0), int(row.solved or 0)) for row in stats
    }

    def bucket(row: ArenaSolveQueueItem) -> int:
        local = local_by_platform.get(str(row.platform_challenge_id or ""))
        if local is None:
            # Unknown box: leave it where the operator put it.
            return HISTORY_NEVER_RUN
        runs, solved = history.get(local, (0, 0))
        if solved > 0:
            return HISTORY_SOLVED_BEFORE
        if runs == 0:
            return HISTORY_NEVER_RUN
        # Few attempts is not a verdict.
        if runs <= PROMISING_RUN_LIMIT:
            return HISTORY_FEW_FAILURES
        return HISTORY_NEVER_SOLVED

    # Stable within a bucket: ``sorted`` preserves the incoming
    # (priority, created_at) order, so the operator's intent still decides ties.
    return sorted(rows, key=bucket)


async def _flagless_platform_ids(
    session: AsyncSession, platform_ids: set[str]
) -> set[str]:
    """Which of these arena challenge ids are marked as carrying no flag.

    The queue stores the *platform* id, while the marker lives on the local
    ``challenges`` row inside ``metadata_json.$.arena_challenge_id``.  Matching
    is done in Python rather than in SQL because ``metadata_json`` is a JSON
    column whose driver representation varies (string for older rows, object for
    newer ones), so a ``JSON_EXTRACT`` predicate would silently miss half of
    them.
    """

    from app.models.challenge import Challenge

    if not platform_ids:
        # An empty ``or_()`` is deprecated (and will raise in a future
        # SQLAlchemy), so the guard has to come before the statement is built.
        return set()

    # Narrow the scan: only rows whose arena id is one of the candidates.  The
    # JSON column is compared as text, which is a superset of the real matches --
    # the exact check below then runs in Python, so an over-broad SQL filter only
    # costs a few rows, never a wrong answer.
    patterns = [f"%{pid}%" for pid in platform_ids]
    rows = (
        await session.scalars(
            select(Challenge).where(
                or_(*(Challenge.metadata_json.like(p) for p in patterns))
            )
        )
    ).all()

    flagged: set[str] = set()
    for row in rows:
        meta = row.metadata_json
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except ValueError:
                continue
        if not isinstance(meta, dict) or not meta.get("no_flag_challenge"):
            continue
        arena_id = str(meta.get("arena_challenge_id") or "").strip()
        if arena_id and arena_id in platform_ids:
            flagged.add(arena_id)
    return flagged

#: Wait before retrying after the platform rate-limits us.  Long enough to fall
#: out of the per-minute window the platform enforces (read 120/min, write
#: 30/min), short enough that a batch does not appear stuck.
RATE_LIMIT_BACKOFF_SECONDS = 65.0

#: Ceiling for the escalating backoff used when the platform is unreachable.
#:
#: Each retry of a five-item batch costs one ``start_environment`` write against
#: the platform's 30/min write budget, so a flat delay kept the batch *inside* the
#: rate limiter: the retries failed for a different reason than the one they were
#: retrying.  Five minutes is long enough to fall out of the per-minute window the
#: platform enforces while still keeping a stalled batch visibly alive.
PLATFORM_BACKOFF_CAP_SECONDS = 300.0

#: Wait before retrying when the platform says its own environment slot is taken.
#:
#: Measured 2026-10-28 15:11 (local): a stale environment from 14:37 was still
#: alive, so every acquire was refused with ``agent_environment_limit``.  Because
#: that code was not recognised as "the slot is taken", all 28 remaining items
#: were retired ``FAILED`` inside two minutes -- none of them was ever evaluated.
#: A short delay lets the platform's environment expire; the item keeps its
#: claim and its outcome budget, so it is retried rather than written off.
PLATFORM_ENV_BUSY_BACKOFF_SECONDS = 30.0

#: Markers that mean the platform rate-limited us: "slow down", not "this
#: challenge is broken".  Matched case-insensitively against the error text; the
#: MCP client surfaces the platform's own code, and ``Too many requests`` is what
#: it says for HTTP 429.
_RATE_LIMIT_MARKERS = (
    "rate_limit_exceeded",
    "too many requests",
    "429",
)

#: Markers that mean the platform could not be reached at all.
#:
#: An unreachable endpoint (or a 502/503 from a gateway in front of it) says
#: nothing about the challenge, yet it used to be charged to the retry budget
#: like a real verdict: measured on the live queue, 10 of 13 failures were
#: ``ARENA_MCP_UNREACHABLE``, so a platform outage was quietly retiring the
#: backlog.  The authoritative signal is the ``DomainError.retryable`` flag the
#: caller passes through; these markers only cover errors re-wrapped into text
#: along the way.
_PLATFORM_UNAVAILABLE_MARKERS = (
    "arena_mcp_unreachable",
    "arena_mcp_http_error",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
)

#: Markers that mean the platform's own single environment slot is occupied.
#:
#: The platform enforces one environment per agent as well; our lease manager
#: covers this host, not the platform's accounting.  When a previous environment
#: has not expired yet, the acquire is refused with the platform's code below --
#: the same condition as ``ARENA_LEASE_BUSY``, just owned upstream, and it says
#: nothing about the challenge.
_PLATFORM_ENV_BUSY_MARKERS = (
    "agent_environment_limit",
    "already runs its maximum number of environments",
)

#: Claim ceiling for failures that are *not* the challenge's fault.
#:
#: Returning an item to ``PENDING`` without touching ``outcome_count`` is what
#: keeps a platform outage from retiring the backlog -- but it also removes the
#: only terminator, so a permanently dead endpoint would leave the scheduler
#: spinning one item per back-off forever while looking "healthy".  This bounds
#: it on the claim counter (already incremented per attempt) rather than the
#: retry budget, so the item is still never retired on a *challenge* verdict --
#: it is retired with an explicit platform reason after a generous number of
#: tries.
PLATFORM_RETRY_MAX_CLAIMS = 20

#: Hard ceiling on one item, watchdog included.
#:
#: ``arena_solve`` already bounds itself -- one Run budget plus a submission
#: margin -- but a wedged HTTP call, a stuck lease poll or an unexpected library
#: hang is exactly the case those bounds do not cover, and an unbounded
#: ``_process`` keeps the platform's only environment slot busy *and* stops the
#: whole batch.
#:
#: Derived from the phases it must clear, not guessed: ready-wait + Run budget +
#: slack for the submission round trips.  A ceiling that sits *below* the worst
#: legitimate spend is worse than none -- it would destroy the environment
#: underneath a slow-but-working solve and turn a success into a timeout.
ARENA_ITEM_WATCHDOG_SECONDS = (
    arena_lease.READY_TIMEOUT_SECONDS + arena_solve.RUN_TIMEOUT_SECONDS + 600
)

#: Back-off when the scheduler cannot even read its own backlog (database blip,
#: pool exhaustion).  An earlier version let that exception escape the loop,
#: which ended the task: ``running`` went False and a multi-hour batch stopped
#: with a single log line.  Retrying on a growing delay converts "the batch is
#: dead" into "the batch waited out a hiccup".
SCHEDULER_ERROR_BACKOFF_SECONDS = 5.0
SCHEDULER_MAX_BACKOFF_SECONDS = 120.0

_FINISHED = {SOLVED, UNSOLVED, FAILED, CANCELLED}


def parse_stop_at(text: str | None) -> datetime | None:
    """Turn an operator-facing cutoff into an aware datetime.

    Two spellings, because both are typed by hand at midnight:

    * full ISO-8601 (``2026-11-03T06:30:00+08:00``), used by anything calling
      the API programmatically; and
    * bare ``HH:MM`` in the host's local time, which is how a person says "stop
      at 6:30".

    A bare time that has already passed means tomorrow -- asking for "06:30"
    at 07:00 should schedule the next 06:30, not stop the batch immediately.
    """

    raw = str(text or "").strip()
    if not raw:
        return None
    bare_time = "T" not in raw and " " not in raw and raw.count(":") == 1
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        if not bare_time:
            raise ValueError(
                f"stop_at must be ISO-8601 or HH:MM, got {raw!r}"
            ) from None
        hours, _, minutes = raw.partition(":")
        try:
            parsed = datetime.now().replace(
                hour=int(hours), minute=int(minutes), second=0, microsecond=0
            )
        except ValueError:
            raise ValueError(f"stop_at is not a valid time: {raw!r}") from None
    if parsed.tzinfo is None:
        # Naive means the operator's wall clock.  Reading it as UTC would fire
        # the cutoff eight hours early (or late) without any visible error.
        parsed = parsed.astimezone()
    if bare_time and parsed <= datetime.now(UTC):
        parsed += timedelta(days=1)
    return parsed


class ArenaQueueScheduler:
    """Single-instance background worker over :class:`ArenaSolveQueueItem`.

    Deliberately not a general job runner.  The only concurrency it must respect
    is the platform's one-slot rule, and ``arena_lease_manager`` already enforces
    that; this class decides *what* to attempt, not *how many* at once.
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._current_item_id: str | None = None
        # Liveness/observability.  A batch runs for hours with no operator
        # watching, so "is the loop actually ticking" has to be readable from the
        # API rather than inferred from a queue that is not moving -- a stopped
        # scheduler and a scheduler waiting on a slow challenge look identical
        # from the outside otherwise.
        self._last_tick_at: float | None = None
        # When the item currently in flight started.  Kept separately from
        # ``_last_tick_at`` because the two answer different questions: "did the
        # loop iterate" versus "how long has this one item been running".
        self._current_started_at: float | None = None
        self._started_at: float | None = None
        self._last_error: str | None = None
        self._consecutive_failures = 0
        self._processed = 0
        self._timeouts = 0
        #: Wall-clock deadline for the batch, if the operator set one.
        #:
        #: A batch runs unattended for hours and nothing else stops it: the
        #: scheduler keeps draining ``PENDING`` until the backlog is empty, which
        #: on 2026-11-02 meant running until 07:50 for a 06:30 cutoff and then
        #: spending the extra time on challenges nobody had budgeted for.  The
        #: deadline is checked between items rather than enforced by ``wait_for``
        #: so a solve in flight is never cut in half -- the same rule ``stop()``
        #: follows, and for the same reason (a cancelled solve can leave the
        #: environment held).
        self._stop_at: datetime | None = None
        self._stop_reason: str | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def current_item_id(self) -> str | None:
        return self._current_item_id

    def health(self) -> dict:
        """Snapshot for the status page.  Pure reads, safe to call any time.

        ``last_tick_age_seconds`` and ``current_item_age_seconds`` are reported
        separately on purpose, and the distinction is the whole point of this
        method.  An earlier version reported only the tick age and called a large
        value "Stalled" -- but during a legitimate item the loop is *inside*
        ``_process`` and not ticking, so a perfectly healthy solve that had been
        running for twenty minutes rendered as "疑似卡死".  A status page that
        cries wolf on the normal case is worse than a silent one: it trains the
        operator to ignore it.

        So: a large tick age with **no** item in flight is the wedge; a large tick
        age **with** an item in flight is just a long item, and the only thing
        that should look alarming there is the age approaching the watchdog, which
        will break it anyway.
        """
        now = time.monotonic()
        item_age = (
            round(now - self._current_started_at, 1) if self._current_started_at else None
        )
        return {
            "running": self.running,
            "current_item_id": self._current_item_id,
            "current_item_age_seconds": item_age,
            "last_tick_age_seconds": (
                round(now - self._last_tick_at, 1) if self._last_tick_at else None
            ),
            "uptime_seconds": (
                round(now - self._started_at, 1)
                if self._started_at and self.running
                else None
            ),
            "processed_items": self._processed,
            "watchdog_timeouts": self._timeouts,
            "consecutive_failures": self._consecutive_failures,
            "last_error": self._last_error,
            "watchdog_seconds": ARENA_ITEM_WATCHDOG_SECONDS,
            # The deadline is reported whether or not it is set: "no deadline"
            # and "deadline already passed" must not look the same to an operator
            # who is not watching the batch.
            "stop_at": self._stop_at.isoformat() if self._stop_at else None,
            "stop_reason": self._stop_reason,
        }

    async def recover(self, *, exclude_item_id: str | None = None) -> int:
        """Reset items stranded in ``RUNNING`` by a crash.  Returns how many.

        A solve is only ever interrupted before an accepted flag exists, so
        returning the item to ``PENDING`` cannot lose a result -- it can only save
        a re-run.  Called at startup for the same reason the lease manager checks
        for orphaned environments.

        ``exclude_item_id`` is what makes this safe to call *while* the scheduler
        runs: the item currently being processed is legitimately ``RUNNING`` and
        must not be yanked out from under the solve that owns it.
        """
        async with SessionLocal() as session:
            stranded = list(
                (
                    await session.scalars(
                        select(ArenaSolveQueueItem).where(ArenaSolveQueueItem.status == RUNNING)
                    )
                ).all()
            )
            repaired = 0
            for item in stranded:
                if exclude_item_id and item.id == exclude_item_id:
                    continue
                item.status = PENDING
                item.last_error = "Interrupted by a restart; returned to the queue."
                repaired += 1
            if repaired:
                await session.commit()
            return repaired

    async def start(self, *, stop_at: datetime | None = None) -> dict:
        if self.running:
            return {"state": "already_running", "current_item_id": self._current_item_id}
        # Sweep before the first tick.  An item left RUNNING by a previous process
        # is invisible to ``_next_item_id`` (it only reads PENDING), so without
        # this a restart would start a batch that silently skips the challenge it
        # was interrupted on.  Cheap, idempotent, and no item is in flight yet.
        with contextlib.suppress(Exception):
            await self.recover()
        self._stop.clear()
        self._started_at = time.monotonic()
        self._last_tick_at = self._started_at
        self._consecutive_failures = 0
        self._last_error = None
        self._stop_at = stop_at
        self._stop_reason = None
        self._task = asyncio.create_task(self._loop(), name="arena-queue-scheduler")
        return {
            "state": "started",
            "stop_at": self._stop_at.isoformat() if self._stop_at else None,
        }

    def _deadline_reached(self) -> bool:
        """Has the operator's cutoff passed?  Timezone-aware, never raises."""

        if self._stop_at is None:
            return False
        now = datetime.now(UTC)
        deadline = self._stop_at
        if deadline.tzinfo is None:
            # A naive deadline is local wall-clock (that is how an operator types
            # "06:30"); interpreting it as UTC would fire eight hours early here.
            deadline = deadline.astimezone()
        return now >= deadline

    async def stop(self) -> dict:
        """Ask the loop to stop after the current item.

        Deliberately does not cancel mid-item: a cancelled solve could leave the
        environment held, and the lease manager's ``finally`` is what destroys it.
        Letting the item finish is slower but cannot leak the slot.
        """
        if not self.running:
            return {"state": "not_running"}
        self._stop.set()
        return {"state": "stopping", "current_item_id": self._current_item_id}

    async def _loop(self) -> None:
        """The batch loop.

        Two structural rules keep a long batch alive:

        * **No exception may end this coroutine.**  Anything raised by the
          read-your-own-backlog step is treated as a transient condition: it is
          recorded, backed off, and retried.  A per-item failure was already
          contained; the loop itself was not, and that is what made a database
          hiccup silently end an hours-long batch.
        * **No item may run unbounded.**  ``wait_for`` bounds each item, so a
          wedged solve cannot hold the environment slot -- or the loop -- forever.
          Cancellation unwinds the ``async with`` lease, which destroys the
          environment on the way out.
        """
        try:
            while not self._stop.is_set():
                # Checked between items, never inside one: cutting a solve in
                # half can leave the environment held, and a challenge that is
                # already most of the way to a flag is worth finishing.
                if self._deadline_reached():
                    self._stop_reason = "STOP_AT_REACHED"
                    logger.info(
                        "Arena queue reached its %s deadline; stopping after %s item(s)",
                        self._stop_at.isoformat() if self._stop_at else "?",
                        self._processed,
                    )
                    self._stop.set()
                    break
                try:
                    item_id = await self._next_item_id()
                except Exception as error:  # noqa: BLE001 - the loop must outlive its data
                    self._consecutive_failures += 1
                    self._last_error = f"{type(error).__name__}: {str(error)[:300]}"
                    delay = min(
                        SCHEDULER_ERROR_BACKOFF_SECONDS * (2 ** (self._consecutive_failures - 1)),
                        SCHEDULER_MAX_BACKOFF_SECONDS,
                    )
                    logger.warning(
                        "Arena queue could not read its backlog (attempt %s); retrying in %.0fs: %s",
                        self._consecutive_failures,
                        delay,
                        self._last_error,
                    )
                    await asyncio.sleep(delay)
                    continue
                self._consecutive_failures = 0
                self._last_error = None
                self._last_tick_at = time.monotonic()
                if item_id is None:
                    await asyncio.sleep(IDLE_SLEEP_SECONDS)
                    continue
                self._current_item_id = item_id
                self._current_started_at = time.monotonic()
                try:
                    await asyncio.wait_for(
                        self._process(item_id), timeout=ARENA_ITEM_WATCHDOG_SECONDS
                    )
                    self._processed += 1
                except TimeoutError:
                    self._timeouts += 1
                    logger.error(
                        "Arena queue item exceeded the %.0fs watchdog item_id=%s",
                        ARENA_ITEM_WATCHDOG_SECONDS,
                        item_id,
                    )
                    # Bookkeeping must not be able to kill the loop: if the
                    # database is what wedged the item, writing its outcome will
                    # fail too, and an exception here would end the batch.
                    with contextlib.suppress(Exception):
                        await self._handle_watchdog_timeout(item_id)
                except Exception:  # noqa: BLE001 - one bad item must not stop the batch
                    logger.exception("Arena queue item failed item_id=%s", item_id)
                    with contextlib.suppress(Exception):
                        await self._mark_failed(item_id, "Unhandled scheduler error.")
                finally:
                    self._current_item_id = None
                    self._current_started_at = None
                    self._last_tick_at = time.monotonic()
        except asyncio.CancelledError:
            raise
        finally:
            self._current_item_id = None
            self._current_started_at = None

    async def _handle_watchdog_timeout(self, item_id: str) -> None:
        """Clean up after an item that outran the watchdog.

        ``wait_for`` cancels the item, which already unwinds the lease's
        ``async with`` and destroys the environment.  Releasing again is
        deliberate belt-and-braces: a lease that survived its task would occupy
        the platform's single slot and make every later challenge fail for a
        reason that points at itself, and ``release`` is idempotent by design.
        """
        with contextlib.suppress(Exception):
            await arena_lease_manager.release()
        await self._mark_failed(
            item_id,
            f"Exceeded the {int(ARENA_ITEM_WATCHDOG_SECONDS)}s watchdog; "
            "the environment was released and the item requeued.",
        )

    async def _next_item_id(self) -> str | None:
        """Pick the next attemptable item.

        Ordered by ``priority`` (the operator's intent, written at enqueue time),
        then age.  The retry budget is filtered here rather than checked after the
        fact so an exhausted item cannot be picked at all.

        The budget is read from ``outcome_count`` (attempts that reached a
        verdict), **not** ``attempt_count`` (times the item was claimed).  Reading
        the claim count here is what let restarts retire challenges: a restart
        returns the item to ``PENDING``, it is claimed again, and two restarts
        exhausted a budget meant for two genuine failures.

        The order deliberately does **not** come from ``has_container``.  An
        earlier version hardcoded offline-first on the assumption that saving the
        single environment slot was always the win -- but an attachment-only
        challenge in this catalogue is usually a teaching exercise with no
        submittable flag, so running those first spent the whole batch on
        challenges that could never score.  Encoding the preference as a priority
        at enqueue time keeps the choice with the caller, who knows whether the
        batch is for scoring or for practice.
        """
        # Yield to anyone else holding the single environment slot.
        #
        # A manual ``POST /arena/solve`` and the batch share one platform slot.
        # Without this the scheduler would keep grabbing items, each of which
        # fails ``ARENA_LEASE_BUSY`` inside ``solve_one``, and every one of those
        # failures burns an ``attempt_count`` -- so a batch started while an
        # operator is poking at a challenge by hand would quietly retire a slice
        # of the backlog for a reason that has nothing to do with the challenges.
        #
        # Safe to read here: this loop releases its own lease before returning to
        # pick the next item, so a held lease at this point is by definition
        # someone else's.
        current = arena_lease_manager.current
        if current is not None and not current.released:
            return None
        async with SessionLocal() as session:
            for item in await self._candidates(session):
                # A second attempt is allowed only if the first was not a hard
                # failure of the platform call itself.
                if item.outcome_count < MAX_ATTEMPTS:
                    return item.id
            return None

    @staticmethod
    @staticmethod
    async def _candidates(session: AsyncSession) -> list[ArenaSolveQueueItem]:
        """Pending items, minus the ones that cannot produce a flag.

        ``no_flag_challenge`` is written into ``challenges.metadata_json`` by
        ``scripts/mark_flagless_challenges.py`` for the teaching exercises whose
        own titles say they carry no flag (``[5.1.3 摩斯密码][无flag]`` and
        ``[5.1.4 仿射密码][无flag]``).  Both had already burned runs reaching
        ``COMPLETED_UNSOLVED`` for a structural reason, not for lack of effort.

        Filtering here rather than at enqueue time because these were enqueued
        before the marker existed, and a queue row can outlive the challenge row
        it points at -- a missing challenge is *not* treated as flagless, so a
        transient lookup failure cannot silently drain the backlog.
        """

        rows = list(
            (
                await session.scalars(
                    select(ArenaSolveQueueItem)
                    .where(ArenaSolveQueueItem.status == PENDING)
                    .order_by(
                        ArenaSolveQueueItem.priority.asc(),
                        ArenaSolveQueueItem.created_at.asc(),
                    )
                )
            ).all()
        )
        if not rows:
            return rows

        rows = await _order_by_history(session, rows)

        platform_ids = {row.platform_challenge_id for row in rows if row.platform_challenge_id}
        if not platform_ids:
            return rows

        flagged = await _flagless_platform_ids(session, platform_ids)
        if not flagged:
            return rows
        kept = [row for row in rows if row.platform_challenge_id not in flagged]
        if len(kept) != len(rows):
            logger.info(
                "arena.queue.skipped_flagless",
                extra={"skipped": len(rows) - len(kept), "platform_ids": sorted(flagged)},
            )
        return kept

    async def _process(self, item_id: str) -> None:
        async with SessionLocal() as session:
            item = await session.get(ArenaSolveQueueItem, item_id)
            if item is None or item.status != PENDING:
                return
            connection = await session.get(ArenaConnection, item.connection_id)
            if connection is None or not connection.enabled:
                await _finish(session, item, FAILED, "Connection is missing or disabled.")
                return
            item.status = RUNNING
            item.started_at = datetime.now(UTC)
            # Claims are counted for the record only; the retry budget reads
            # ``outcome_count``, which a claim must not touch.  Charging the
            # budget here is what let process restarts retire healthy challenges.
            item.attempt_count += 1
            await session.commit()
            practice_ground_id = item.practice_ground_id
            challenge_id = item.platform_challenge_id

        # A separate session for the solve: it runs for many minutes and must not
        # hold open the short transaction that claimed the item.
        async with SessionLocal() as solve_session:
            try:
                outcome = await arena_solve.solve_one(
                    solve_session,
                    connection=connection,
                    practice_ground_id=practice_ground_id,
                    challenge_id=challenge_id,
                    start_run_fn=arena_solve.start_run_for_arena,
                    on_run_created=lambda run_id: self._record_run_id(item_id, run_id),
                )
            except Exception as error:  # noqa: BLE001 - recorded on the item, not raised
                # ``retryable`` is the structured half of "was this the platform's
                # fault?" (``DomainError`` sets it from the HTTP status).  Passing
                # it through keeps an outage from being charged to the retry
                # budget even if the message text is re-wrapped along the way.
                await self._mark_failed(
                    item_id,
                    str(error)[:1000],
                    retryable=getattr(error, "retryable", None),
                )
                return

        async with SessionLocal() as session:
            item = await session.get(ArenaSolveQueueItem, item_id)
            if item is None:
                return
            item.run_id = outcome.run_id or item.run_id
            item.verdict = outcome.verdict
            # A Run that ended because no engine was usable never attempted the
            # challenge, so it is not a verdict on it.  Returning the item to the
            # backlog (claim undone, ``outcome_count`` untouched) is what keeps
            # an upstream outage from silently retiring the whole batch as
            # "unsolved" -- measured 2026-10-25: four consecutive runs closed
            # ``NO_HEALTHY_WORKER_PROFILE`` with 0 worker steps each, and all
            # four were filed UNSOLVED, so none would ever be retried once the
            # relay came back.
            if _is_infrastructure_no_attempt(outcome.run_status_reason):
                item.attempt_count = max(0, item.attempt_count - 1)
                await _finish(
                    session,
                    item,
                    PENDING,
                    "No usable Worker engine for this attempt; "
                    "the item was returned to the queue rather than charged "
                    "as an unsolved challenge.",
                )
                logger.warning(
                    "Arena queue item deferred, no healthy Worker engine item_id=%s reason=%s",
                    item_id,
                    outcome.run_status_reason,
                )
                return
            # The solve produced a verdict, so this attempt is spent.  Recorded
            # before the status is written so the two cannot disagree in the
            # listing -- an item whose status is UNSOLVED must show a nonzero
            # outcome count, or the next read looks like it never ran.
            item.outcome_count += 1
            status = _status_for(outcome.verdict)
            await _finish(session, item, status, None)
            # Captured while the session still owns the row: ``_finish`` commits,
            # and reading an expired attribute after the block would raise.
            started_run_id = item.run_id
        # A verdict is not the same as a closed Run: ``wait_for_run`` gives up on
        # its own timeout and returns ``TIMEOUT_WAITING`` while the Run is still
        # alive, so an unsolved or timed-out item can leave a Run behind.  Closing
        # it here is a no-op when the Run already finished (the solved case).
        if status != SOLVED:
            await _abandon_run(
                started_run_id,
                "The arena queue recorded a verdict for this item; the Run it "
                "started is superseded.",
            )

    async def _record_run_id(self, item_id: str, run_id: str) -> None:
        """Attach the Run id to its queue row the instant the Run exists.

        The row is otherwise only written back after the solve returns, so a
        solve that legitimately runs for twenty minutes showed ``run_id: None``
        for its whole duration -- indistinguishable from an item that never
        reached the Run-creation step, and impossible to open and inspect from the
        queue.  Deliberately best-effort: losing this write costs a link, and it
        must never turn into a failure of the solve holding the environment.
        """
        async with SessionLocal() as session:
            item = await session.get(ArenaSolveQueueItem, item_id)
            if item is None:
                return
            item.run_id = run_id
            await session.commit()

    async def _mark_failed(
        self, item_id: str, message: str, *, retryable: bool | None = None
    ) -> None:
        backoff_seconds = 0.0
        async with SessionLocal() as session:
            item = await session.get(ArenaSolveQueueItem, item_id)
            if item is None:
                return
            # The Run this item started, captured before anything is written.
            # Whichever branch below runs, the item is moving on -- to a retry or
            # to a give-up -- so that Run is superseded and must be closed rather
            # than left for the supervisor to resume.  See ``_abandon_run``.
            superseded_run_id = item.run_id
            if _is_busy_error(message):
                # Someone else holds the single environment slot.  This says
                # nothing about the challenge, so the item goes back to the
                # backlog *with its claim undone* -- counting it would let a
                # handful of manual solves exhaust a budget meant for genuine
                # failures.  Only the claim is restored: the retry budget reads
                # ``outcome_count``, which this path never touched.
                item.attempt_count = max(0, item.attempt_count - 1)
                await _finish(session, item, PENDING, message)
                if _is_platform_env_busy(message):
                    # No local lease is held, so nothing on our side will release
                    # this on the next tick -- only the platform's own environment
                    # expiring will.  Sleeping keeps the loop from re-picking the
                    # same item in a tight retry storm while it waits.
                    backoff_seconds = PLATFORM_ENV_BUSY_BACKOFF_SECONDS
                logger.info("Arena queue yielded to a held environment slot item_id=%s", item_id)
            elif _is_rate_limit_error(message):
                # Rate limiting is the platform asking us to slow down, not a
                # verdict on the challenge.  It must not consume an attempt: a
                # burst of our own requests (a batch plus manual probes) can trip
                # it, and retiring every queued challenge over that would make a
                # batch fail for a reason that has nothing to do with the
                # challenges.  The delay is the point -- retrying immediately is
                # what caused the limit in the first place.
                #
                # The claim is undone because the request was refused before it
                # could use the environment at all.
                item.attempt_count = max(0, item.attempt_count - 1)
                await _finish(session, item, PENDING, message)
                logger.info(
                    "Arena queue item rate-limited; backing off %.0fs item_id=%s",
                    RATE_LIMIT_BACKOFF_SECONDS, item_id,
                )
                backoff_seconds = RATE_LIMIT_BACKOFF_SECONDS
                backoff_seconds = RATE_LIMIT_BACKOFF_SECONDS
            elif _is_platform_unavailable(message, retryable):
                # The platform itself did not answer (unreachable endpoint, or a
                # 502/503 from a gateway in front of it).  That is not a verdict
                # on the challenge, so ``outcome_count`` -- the budget that
                # retires items -- must not move; otherwise an outage quietly
                # retires the whole backlog.  Unlike a rate limit the request was
                # not refused up front, so the claim stands and doubles as the
                # retry counter for this path.
                if item.attempt_count >= PLATFORM_RETRY_MAX_CLAIMS:
                    await _finish(
                        session,
                        item,
                        FAILED,
                        (
                            f"{message} — retired after {item.attempt_count} attempts "
                            "without the platform ever answering; the challenge was "
                            "never evaluated."
                        )[:1000],
                    )
                    logger.warning(
                        "Arena queue item retired after %d unreachable attempts item_id=%s",
                        item.attempt_count,
                        item_id,
                    )
                else:
                    await _finish(session, item, PENDING, message)
                    # Escalating, not flat.  Every retry spends one
                    # ``start_environment`` call against the platform's own rate
                    # budget, and a flat 65s was tight enough that a five-item
                    # batch whose first item kept failing created a *second*
                    # outage out of its own retries: the next run then failed
                    # with ``RATE_LIMIT_EXCEEDED`` instead of the environment
                    # error it was retrying.  Backing off geometrically keeps the
                    # batch honest -- the first retry still happens inside the
                    # normal window, the tenth waits long enough for the platform
                    # to have recovered.
                    backoff_seconds = min(
                        RATE_LIMIT_BACKOFF_SECONDS * (2 ** max(0, item.attempt_count - 1)),
                        PLATFORM_BACKOFF_CAP_SECONDS,
                    )
                    logger.info(
                        "Arena queue item could not reach the platform; backing off "
                        "%.0fs (attempt %d/%d) item_id=%s",
                        backoff_seconds,
                        item.attempt_count,
                        PLATFORM_RETRY_MAX_CLAIMS,
                        item_id,
                    )
            else:
                # A failure that reached this point *is* a spent attempt: the
                # solve ran and did not produce a usable result, which is exactly
                # what the budget is for.  Counted before the status so the
                # listing cannot show a FAILED item with a zero outcome count.
                item.outcome_count += 1
                status = PENDING if item.outcome_count < MAX_ATTEMPTS else FAILED
                await _finish(session, item, status, message)
        await _abandon_run(
            superseded_run_id,
            "The arena queue moved on to a new attempt; this Run is superseded.",
        )
        if backoff_seconds:
            await asyncio.sleep(backoff_seconds)


async def _abandon_run(run_id: str | None, reason: str) -> None:
    """Close the Run this queue item created, now that the item has moved on.

    A retry, a watchdog timeout, or a failed solve all leave the previous Run
    behind -- and a Run parked in a pause state is *not* inert.  The Run
    supervisor sweeps every ``PAUSED_RECOVERY`` / ``PAUSED_CHECKPOINT`` row and
    resumes it, so a superseded Run comes back to life and solves the same
    challenge a second time: against a target the platform has since
    re-provisioned, while the retry holds the single environment slot, both
    burning Worker credits.  Nothing about that shows up as an error -- the
    queue looks healthy, the Run looks like it is "recovering".

    The queue created the Run, so the queue closes it, through the same
    lifecycle the cancel endpoint uses (a Muteki Run additionally needs its own
    Coordinator/Finalize handoff so the Worker sandbox is torn down, not just
    the database row).

    Deliberately best-effort: item accounting must not fail because a Run could
    not be closed, and a Run that is already terminal is left untouched.
    """
    if not run_id:
        return
    try:
        from app.models.run import SolveRun
        from app.orchestration.state_machine import TERMINAL, parse_run_status
        from app.services.run_lifecycle import cancel_run

        async with SessionLocal() as session:
            run = await session.get(SolveRun, run_id)
            if run is None:
                return
            # Tolerant read: an unreadable status means "not terminal", which
            # keeps the row eligible to be closed instead of skipped forever.
            parsed = parse_run_status(run.status)
            if parsed is not None and parsed in TERMINAL:
                return
            solver_mode = str(run.solver_mode or "").lower()
        if solver_mode == "muteki":
            from app.services.run_supervisor import run_supervisor

            with contextlib.suppress(Exception):
                await run_supervisor.cancel_muteki_with_handoff(
                    run_id, await_finalize=False
                )
        async with SessionLocal() as session:
            await cancel_run(session, run_id, reason)
    except Exception:  # noqa: BLE001 - bookkeeping must never fail the item
        logger.exception("Arena queue could not close superseded Run run_id=%s", run_id)


def _is_busy_error(message: str) -> bool:
    """Whether this failure is "the one environment slot is taken".

    Distinguished from a generic transient error because the timing differs: a
    rate limit wants a long back-off, a held slot wants the next idle tick.

    Both owners of the slot are covered.  ``ARENA_LEASE_BUSY`` is this host's
    lease manager; ``agent_environment_limit`` is the platform refusing because
    an environment it still counts as alive is occupying the slot.  The second
    one used to fall through to the generic "spent attempt" branch, which retired
    28 queued items in two minutes on 2026-10-28 without evaluating any of them.
    """
    return any(
        marker in str(message or "")
        for marker in (
            "ARENA_LEASE_BUSY",
            "ARENA_SOLVE_BUSY",
            *_PLATFORM_ENV_BUSY_MARKERS,
        )
    )


def _is_platform_env_busy(message: str) -> bool:
    """Whether the *platform* (not this host's lease) is holding the slot."""

    text = str(message or "").casefold()
    return any(marker.casefold() in text for marker in _PLATFORM_ENV_BUSY_MARKERS)


def _is_rate_limit_error(message: str) -> bool:
    """Whether the platform asked us to slow down rather than rejecting the work.

    Kept separate from the retry counter because the two mean different things: a
    genuine failure should retire the item after ``MAX_ATTEMPTS``, while rate
    limiting should simply wait -- it says nothing about the challenge and would
    otherwise retire a whole batch over our own request burst.
    """

    text = str(message or "").casefold()
    return any(marker in text for marker in _RATE_LIMIT_MARKERS)


def _is_platform_unavailable(message: str, retryable: bool | None = None) -> bool:
    """Whether the platform never answered, so the challenge was not evaluated.

    ``retryable`` is ``DomainError.retryable`` when the caller still has the
    exception: it is the authoritative structured signal (5xx / 429 are marked
    retryable by the MCP client), and it survives being re-wrapped into text by
    the markers above.
    """

    if retryable is True:
        return True
    text = str(message or "").casefold()
    return any(marker in text for marker in _PLATFORM_UNAVAILABLE_MARKERS)


def _status_for(verdict: str | None) -> str:
    """Map a solve verdict onto a queue status.

    ``already_solved`` counts as SOLVED: the goal state is reached, and marking it
    otherwise would make a batch look like it was failing.
    """
    if verdict in {"accepted", arena_solve.VERDICT_ALREADY_SOLVED}:
        return SOLVED
    if verdict == "no_candidate":
        return UNSOLVED
    return UNSOLVED


#: Run reasons that mean the solve never got a chance to work on the challenge.
#:
#: These are infrastructure verdicts, not challenge verdicts.  ``NO_HEALTHY_WORKER_
#: PROFILE`` is the loudest: every configured engine failed its PREPARE health
#: check, so the Coordinator closed the Run before creating a single Intent --
#: measured 2026-10-25 on four consecutive runs (``88eacfa8``, ``75c89026``,
#: ``1823ca8c``, ``a299a70b``), each burning ~250 seconds and 0 worker steps.
#:
#: Charging those to the challenge is actively harmful in two ways: the queue
#: retires the item as UNSOLVED so it is never retried once the outage clears,
#: and the batch statistics read as "four Easy Web boxes we could not solve"
#: when in fact nothing was ever attempted.
INFRASTRUCTURE_NO_ATTEMPT_REASONS: tuple[str, ...] = (
    "NO_HEALTHY_WORKER_PROFILE",
    "CREDENTIAL_RESOLUTION_FAILED",
    # A Worker that never ran a single command -- the CLI reconnected in a loop
    # and the Run hit its no-progress bound (``RUN_STALL_GRACE_SECONDS``).  Like
    # the two above, this is a verdict on the transport, not on the challenge:
    # charging it would retire a box the solver never actually looked at.
    "WORKER_STALL_NO_COMMANDS",
)


def _is_infrastructure_no_attempt(reason: str | None) -> bool:
    """True when the Run ended because no engine was usable, not because it tried."""

    text = str(reason or "").strip().upper()
    if not text:
        return False
    return any(marker in text for marker in INFRASTRUCTURE_NO_ATTEMPT_REASONS)


async def _finish(
    session: AsyncSession, item: ArenaSolveQueueItem, status: str, message: str | None
) -> None:
    item.status = status
    if message:
        item.last_error = message
    # Only a terminal status closes the item; one returned to PENDING has not
    # finished, so it must not look finished in the listing.
    item.finished_at = datetime.now(UTC) if status in _FINISHED else None
    await session.commit()


arena_queue_scheduler = ArenaQueueScheduler()
