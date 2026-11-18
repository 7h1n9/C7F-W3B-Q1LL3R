from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from .adapter.official_observation import (
    SafeNativeObservation,
    extract_native_observations,
)
from .container_exec import ContainerExecutor
from .control import ControlReceiver
from .core.stage_policy import StagePolicy
from .events import EventType
from .gate import DEFAULT_FLAG_PATTERN
from .graph import MutekiGraph
from .outcomes import (
    WorkerResultCode,
    failure_code,
    signal_from_worker_text,
    stable_branch_id,
    stable_route_hash,
)
from .phases import MutekiPhase
from .reason import MutekiReason, ReasonResult
from .semantic_dispatch import (
    SemanticReservation,
    acquire_dispatch_semantics,
    release_dispatch_semantics,
)
from .worker.chat_health import probe_chat_worker
from .worker.official_worker import (
    OfficialWorkerAdapter,
    OfficialWorkerConfig,
    OfficialWorkerResult,
)
from .worker.review_worker import ReviewWorker, is_verification_unit
from .workers import EngineProfile, MutekiWorkerPool, WorkerJob, WorkerOutcome

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .cli_driver import CLIDriver


_RACE_LANES: tuple[dict[str, str], ...] = (
    # The first lane is an *exploitation* lane, not a reconnaissance one.  The
    # arena roster is mostly one-CVE-per-box vulhub images (``[PHPMYADMIN]
    # CVE-2018-12613``), and the workspace ``AGENTS.md`` now carries the curated
    # exploit path for the declared CVE.  Putting recon first meant every Race
    # spent its whole bounded window enumerating robots.txt / headers / 403
    # directories and timed out (``RACE_WORKER_TIMEOUT``) with the exploit never
    # attempted -- the facts read like progress while the flag stayed untouched.
    # The lane stays honest: it points the Worker at ``AGENTS.md`` and falls
    # back to exploration when there is no recipe, so an unannotated box behaves
    # exactly as before.
    {
        "lane_key": "exploit:known-path",
        "label": "execute the known exploit path for this challenge",
        "goal_template": (
            "Exploitation lane: {label}. Look up the recipe FIRST, do not scan. "
            "1) Read ./AGENTS.md — if it has a 'Known exploit path' section, send "
            "that payload now. "
            "2) Else if the title/brief names a CVE: rg -l 'CVE-YYYY-NNNN' "
            "/home/kali/pocs/vulhub | head, then cat the README it prints (it holds "
            "the working request for this exact image) and send it. "
            "3) Only if both are empty, explore normally. "
            "Do NOT spend the window on robots.txt, response headers, version "
            "banners, 403 directories or login crawling: the window closes and the "
            "flag was never requested."
        ),
    },
    {
        "lane_key": "recon:public-endpoints",
        "label": "public endpoints and framework surface",
    },
    {
        "lane_key": "recon:session-api",
        "label": "session, authentication and API surface",
    },
    {
        "lane_key": "recon:business-objects",
        "label": "business objects, identifiers and workflow surface",
    },
    # The one lane that does not run in the target at all.
    #
    # ``lookup_public_poc`` is a *host-side* function -- deliberately, so the
    # Worker container never needs egress beyond the challenge target -- and it
    # is served only over the ``chat_completions`` protocol.  Measured over 484
    # Runs it was called **zero** times, because every Run was dispatched to CLI
    # Workers that cannot reach it.  Naming it in the CLI goal was an
    # instruction that could never be obeyed.
    #
    # Rather than delete the capability, this lane gives it a Worker that can
    # actually call it: an OpenAI-compatible engine with the tool bound.  It
    # reads the *fingerprint the exploit lane already observed*, turns it into
    # a CVE/product lookup, and writes what it finds (plus any curated recipe
    # the lookup returns) onto the board for the exploit lane to use.
    #
    # ``requires_protocol`` is what keeps this honest: if no chat-completions
    # engine is healthy, the lane is skipped instead of being handed to a CLI
    # Worker that would again be unable to obey.
    {
        "lane_key": "intel:public-poc",
        "label": "public CVE and PoC lookup for the fingerprint already observed",
        "requires_protocol": "chat_completions",
        "goal_template": (
            "Intelligence lane: {label}. Do NOT attack the target; another lane "
            "does. 1) Read the board for the fingerprint already observed "
            "(`blackboard.py read-facts`): product, version, Server. No re-probe. "
            "2) With a product AND version, call `lookup_public_poc` with "
            "'product major.minor.patch' as seen; read `match_on.axis` "
            "(`cpe_version` = NVD lists it affected, `keyword_product_only` = "
            "unconfirmed). 3) Publish each hit as `VERIFIED_FACT=` with the CVE "
            "and axis -- a lookup is a hypothesis. 4) No fingerprint? Say what you "
            "wait for, do not scan."
        ),
    },
)

# Used when only one engine is healthy: the lane that has to pay off is the
# exploit lane, not a whole-challenge reconnaissance pass that runs out of
# window before anything is exploited.
#
# Kept under ~380 chars on purpose.  ``muteki_runtime`` truncates the intent goal
# at 600 chars, and the backfill prefix below is prepended to this text -- a
# longer template used to lose its own tail ("the flag was never requested")
# exactly when the single-engine path needed that sentence most.
_SINGLE_WORKER_GOAL = (
    "Only you are running. "
    "1) Read ./AGENTS.md; if it has a 'Known exploit path', send that payload now. "
    "2) Else fingerprint in ONE command: Server/X-Powered-By, product+version, "
    "app paths. Got a product AND version? Read the LOCAL corpus in "
    "/home/kali/pocs (vulhub + Awesome-POC), follow what matches. "
    "3) Still nothing? Explore normally. "
    # ``lookup_public_poc`` used to be named here.  It is a host-side function
    # served only over the chat_completions protocol, and this goal ships to
    # CLI Workers, which run inside the container and cannot reach it.  Measured
    # across 484 Runs: **zero** calls, in solved and unsolved alike -- an
    # instruction that could never be obeyed.  The local corpus is the same
    # information in a form these Workers can actually read; the Drupal Run
    # (``e92b8431``) solved its box after 22 reads of it.
    #
    # This text is capped at ~420 chars: ``[BACKFILL]`` (180) is prepended when
    # another engine is down, and the intent goal truncates at 600.
    "Do not crawl robots.txt, 403s or login pages, and do not repeat the fingerprint."
)

# Prepended to a healthy engine's goal when another engine failed its health
# check.  The Run must not shrink to "one lane" silently: the surviving engine
# is told it also owns the missing lane, and -- crucially -- what to do *after*
# the exploit attempt fails, because that second half used to be nobody's job.
_BACKFILL_GOAL_PREFIX = (
    "[BACKFILL] {missing} engine(s) down; you also own their lane. "
    "If the exploit attempt fails, pivot to public endpoints, auth/API and "
    "business objects instead of repeating the same request. "
)


def _probe_goal(probe: str, fallback: str) -> str:
    """Word an unmeasured recipe's first move as a probe, not an order.

    The distinction is the whole point of this channel.  A recipe with an
    accepted flag behind it is stated as an order ("run it as your very first
    command") because following it is known to work on this platform.  A recipe
    imported from vulhub has produced a flag on somebody's box, not necessarily
    on this instance -- the S2-009 ``/ajax/example5`` entry passed every schema
    check and was simply wrong here.

    So the probe says three things, in this order, because that is the order a
    Worker needs them: (1) send this specific request first, (2) expect it to
    fail and that is a normal outcome, (3) what to do when it does.  The explicit
    "a failure is the expected outcome" is what stops the round from being spent
    forcing a recipe that does not fit -- the exact failure mode that made the
    trust gate exist in the first place.

    ``fallback`` (the technique clause) is appended when present so that a failed
    probe still leaves a decision procedure rather than a blank instruction.
    """

    head = (
        "Untested lead for this box — send it as your first command, then judge it "
        f"by the response: {probe} "
        "This recipe is from the public corpus and was NOT verified on this "
        "instance, so a 404/garbage/unchanged response is a NORMAL outcome, not a "
        "failure — if that happens, mark it a dead end and move on immediately "
        "instead of reworking the same request. "
    )
    if fallback:
        head += "If it does not confirm, fall back to what to look for next: " + fallback
    return head


_CONTAINER_WORKSPACE = "/home/kali/workspace"
_ACTIVE_INTENT_STATUSES = {"open", "claimed", "in_progress", "dispatched"}
# Finalize closes every active direction itself: an ``open`` Intent is claimed
# by the Coordinator first, exactly like a Worker would, and only then
# concluded.  The lease is a pure formality -- the Run is already terminal --
# so it is deliberately short instead of the 300s Worker lease.
FINALIZE_INTENT_LEASE_SECONDS = 30.0
# How long a cancelled Finalize keeps absorbing further cancellations while it
# finishes its terminal steps.  Long enough for a real Worker/Sandbox reclaim to
# land after a Run deadline, and deliberately shorter than the Runtime's own
# Finalize reclaim bound (``muteki_runtime.FINALIZE_RECLAIM_SECONDS``) so a
# Coordinator whose cleanup is genuinely wedged stays reclaimable.
FINALIZE_CANCEL_GRACE_SECONDS = 5.0
# Hard ceiling for the stop-convergence window.  ``CoordinatorConfig`` exposes
# the budget per Run, but a Run cannot spend more than this on convergence: the
# Runtime awaits the whole Finalize under its own grace bound
# (``muteki_runtime.FINALIZE_GRACE_SECONDS = 180s``) and Finalize still has to
# cancel the Worker, reap the Sandbox and publish the terminal event inside it.
# Anything above this cap would trade "verification completed" for "terminal
# event never landed", which is strictly worse.
STOP_CONVERGENCE_MAX_SECONDS = 120.0

#: The terminal reason used when a Worker round hit its own deadline.
#: It is the value ``MutekiOrchestrator.run`` already recognises as "time ran
#: out, not an operator stop", which is what maps the Run onto ``TIMEOUT``
#: rather than ``COMPLETED_UNSOLVED``.  Kept identical to the dead-end
#: description of the same event so an audit trail greps consistently.
TIMED_OUT_STOP_REASON = "RACE_WORKER_TIMEOUT"


@dataclass(frozen=True, slots=True)
class _RefusedCandidate:
    """A gate decision for a value we keep but do not accept.

    Only exists so recovered values can be written through the graph's single
    flag-writing boundary (``record_flag_decision``) as ``FLAG_CANDIDATE``
    entries.  It is never accepted: a value whose banner had to be supplied by
    the host is a candidate for review, not a solve.
    """

    flag: str
    accepted: bool = False
    reason: str = "RECOVERED_NOT_ACCEPTED"

# The prepare-phase health probe is a real model turn per engine, so a transient
# upstream failure is indistinguishable from a dead engine.  A relay that resets
# a connection can then refuse every request for minutes ("security policy"),
# which is far longer than one probe round -- so the retry window is bounded by
# TIME, not by attempt count, and sized to outlast such a window.  Abandoning a
# Run before its first Intent costs far more than a few extra probe turns.
HEALTHCHECK_MAX_WAIT_SECONDS = 240.0
HEALTHCHECK_RETRY_DELAY_SECONDS = 5.0

#: How long a reactivation probe waits for the Sandbox to go idle.
#:
#: The probe must not compete with a live Worker for the one container, so it
#: polls ``pool.active_count``.  Without a ceiling that poll can outlast the Run:
#: measured on ``4666646b``, the probe was scheduled and then never fired while
#: the single surviving lane kept producing work, so the Run never regained its
#: second engine.  Waiting a bounded amount and giving up turns an unbounded
#: stall into a missed bonus.
HEALTHCHECK_IDLE_WAIT_SECONDS = 90.0

# Worker steps arrive on EVERY parsed stream line, so a long Worker round can
# produce thousands of them.  Persisting one RunEvent per line would bloat the
# durable stream (and the SSE fan-out) far more than it buys, so steps are
# coalesced per Worker: an event is published when the step KIND changes (so the
# reasoning -> tool -> tool_result sequence stays visible) or when the cadence
# below elapses, with per-window aggregates carried in the payload.  This keeps
# "why did this Worker fail?" answerable without turning observability into the
# dominant write load.
WORKER_STEP_MIN_INTERVAL_SECONDS = 5.0
WORKER_STEP_MAX_TOOLS = 12
# Tool invocations that carry a command + output are published one event each
# (never coalesced) because the aggregate cannot answer "which command caused
# this dead end".  A 720s CLI round runs 20-70 tool calls, so this ceiling is
# ~6x the observed worst case: it exists only to stop a pathological python_run
# loop from turning the durable stream into a log sink.
WORKER_COMMAND_MAX_PER_WORKER = 400

# How many consecutive Worker rounds may count as "progress" on the strength of
# unverified facts alone.  A Worker's own fact is written with ``verified=False``
# and its ``verifier`` is that same Worker -- the 2026-10-21 phpMyAdmin run
# reported "achieved RCE as www-data, PWN42 marker executed" at confidence 0.4
# and never actually had a shell, yet the Coordinator treated the round as
# progress and kept spending the budget in the same direction.
#
# Three rounds is deliberately generous: a genuine solve normally lands a
# verified flag or a host-verified fact well before this, and it stays far below
# ``barren_limit`` (8) so no Run is cut short by this rule alone.
UNVERIFIED_ONLY_ROUNDS_LIMIT = 3

#: How long the fact count behind the stagnation bonus may be reused.
#:
#: The count is read once per Worker command from the Worker's own thread, and
#: ``graph.facts()`` is a SQLite query over a growing table.  Facts are written by
#: that same thread, so there is no separate write hook to invalidate against; a
#: short TTL is used instead.  Two seconds of staleness can delay the bonus by at
#: most one command, which cannot matter for a budget measured in tens.
_STAGNATION_FACT_TTL_SECONDS = 2.0


def _container_graph_path(graph_path: str | Path) -> str:
    """Map the host graph path to the path visible inside the Worker container."""

    path = Path(graph_path)
    if path.parent.name.casefold() == "graph":
        relative = Path("graph") / path.name
    else:
        try:
            root = path.parent.parent.resolve()
            relative = path.resolve().relative_to(root)
        except (OSError, ValueError):
            relative = Path("graph") / path.name
    return f"{_CONTAINER_WORKSPACE}/{relative.as_posix()}"


@dataclass(frozen=True, slots=True)
class CoordinatorConfig:
    interval_seconds: float = 2.0
    max_workers: int = 10
    race_enabled: bool = True
    # Explicit bounded callers may still set this value.  Production Runtime
    # passes ``unbounded=True`` and uses the Run-level total-runtime deadline,
    # matching upstream Muteki's graph-driven Coordinator loop.
    max_ticks: int = 100
    review_interval: int = 3
    stage_policy: StagePolicy | dict | None = None
    worker_backend: str = "callback"
    worker_timeout_seconds: int = 480
    # Per-Worker CLI turns remain the native Muteki Worker budget.  The
    # Coordinator-wide spawn budget below is separate, just like upstream.
    worker_max_turns: int = 80
    # Upstream Muteki charges every newly spawned Worker against
    # ``max_total_workers`` before it starts the Worker task.  Keep the same
    # explicit boundary in the compatibility Coordinator so a production Run
    # cannot turn a barren route into an unbounded bootstrap loop.
    max_total_workers: int | None = None
    # The Worker owns the execution deadline.  Keep a small separate grace
    # window for process teardown/control-plane reporting so a result produced
    # exactly at the Worker deadline is not misclassified as a Coordinator
    # timeout.
    worker_wait_grace_seconds: float = 5.0
    # Upstream Muteki's barren_limit: after this many consecutive worker
    # completions with NO new fact (incl. candidates) and NO new flag, the
    # coordinator stops spawning new workers instead of burning tokens.
    # 0 disables.  (upstream default: 8)
    barren_limit: int = 8
    # Commands since the last new fact/dead-end before the Coordinator asks to
    # converge.  Complements ``no_progress_timeout`` (which is wall-clock) and
    # ``barren_limit`` (which counts whole rounds): a Worker can spend hundreds
    # of commands inside a single round and never trip either.  Measured on
    # 2026-11-06, UNSOLVED runs produced nothing new for the last 28% of their
    # command stream, and one run re-ran the same ``find`` with a larger
    # ``--max-time`` ten times after it had already written the dead-end.
    # 0 disables.
    stagnation_command_limit: int = 40
    #: Reward forward progress with a longer stagnation allowance.
    #:
    #: The flat 40 above is calibrated on *shallow* Runs: it stops a Worker that
    #: is looping on one endpoint.  It also stops a Worker that is mid-way up a
    #: multi-stage chain, because the steps between stages produce no fact by
    #: construction -- deriving a filename oracle, computing an md5, compiling a
    #: shared object -- and 40 commands of that look identical to 40 commands of
    #: flailing.
    #:
    #: Measured 2026-11-08 over the 46 UNSOLVED non-CVE Web Challenges: **72%**
    #: of their last Runs had already produced >=2 real technical conclusions,
    #: and 20 of 46 had burned all three retry attempts.  The representative case
    #: (`[CISCN2019 总决赛 Day1 Web1]滑稽云音乐`) had leaked the admin password,
    #: confirmed the firmware upload and derived the filename oracle, then died
    #: constructing the ELF object during convergence.  Those boxes are 3-5 hops
    #: deep; the CVE boxes that do get solved are 1-2.
    #:
    #: So once a Run has *demonstrated* it can produce facts, the allowance grows
    #: with the number of facts it has produced.  A Run that has produced nothing
    #: keeps exactly the old 40 -- the fast-fail case is unchanged, which is what
    #: keeps CVE Runs (already 68% solved) from getting slower.
    #:
    #: 0 disables the growth and restores the flat budget.
    stagnation_progress_bonus_per_fact: int = 20
    #: Ceiling for the grown allowance.  40 + 5*20 = 140 commands of slack before
    #: convergence is requested, which is roughly one deep chain hop per fact.
    #: Bounded so a Run cannot spin forever just by writing cheap facts.
    stagnation_command_limit_max: int = 140
    #: Facts that earn no bonus at all -- the threshold a Run must clear before
    #: any runway is granted.  One fact can be a bare fingerprint ("nginx 1.18");
    #: two is the point where a Worker has shown it builds on its own findings.
    #: The bonus applies to facts ABOVE this line, so a Run with exactly 2 facts
    #: still gets the flat budget: the reward is for demonstrated *momentum*, not
    #: for crossing the bar once.
    stagnation_progress_bonus_min_facts: int = 2
    #: Same counter, but the budget applied once the Worker has written a
    #: ``DEADEND=`` marker.  A stated dead-end is the Worker telling us a
    #: direction is finished, so the slack that is reasonable *before* knowing
    #: anything should not be granted again *after* it has said what does not
    #: work.  Measured: one run wrote its dead-end at command 26 of 36 and then
    #: spent ten more re-running the same search with a longer timeout.
    #: 0 keeps a single flat budget.  Must be <= ``stagnation_command_limit``.
    stagnation_after_deadend_limit: int = 12
    #: How many *identical* commands in a row trigger convergence.
    #:
    #: The fact-based budget above cannot see a Worker that re-sends the same
    #: request: every probe that returns anything produces a ``VERIFIED_FACT``,
    #: which resets ``_commands_since_progress``, so a Run can loop on one request
    #: forever and still look like progress.
    #:
    #: Measured 2026-11-09 on ``[BUUCTF 2018]Online Tool``: 264 commands against
    #: one target, 131 of them (~50%) sharing the first 60 characters with another
    #: -- the identical ``curl -sS "http://<target>/...`` prefix appeared 70 times,
    #: and one ``cd /tmp; U=...`` trampoline 46 times.  That Run ended UNSOLVED
    #: with a single fact after 139 commands while solved ordinary boxes of the
    #: same family finish with 4-17 facts.
    #:
    #: Five is deliberately short: a command repeated five times *in immediate
    #: succession* is a loop by any reading, and unlike the fact counter this
    #: cannot be reset by output that merely looks like knowledge.  0 disables.
    stagnation_repeat_limit: int = 5
    #: How many leading characters identify "the same command".  Sixty is enough
    #: to capture the URL and verb that distinguish two probes, while ignoring
    #: arguments that legitimately vary (payload bytes, --max-time values).
    stagnation_repeat_fingerprint_chars: int = 60
    # No-progress wall-clock window: if no new fact/flag has been published
    # for this many seconds, the coordinator treats the run as stalled and
    # stops. 0 disables.  (upstream default: 1800)
    no_progress_timeout: float = 0.0
    official_account_root: str | None = None
    # Official Muteki keeps re-bootstrap bounded by Coordinator policy.  The
    # compatibility runtime exposes the same safety boundary.  Zero means
    # "no separate bootstrap cap"; the outer tick/runtime budget remains the
    # safety boundary.  This matches the upstream Coordinator, which keeps
    # re-bootstrap available after a barren route instead of silently ending
    # the solve after one whole-challenge Worker.
    max_bootstrap_workers: int = 0
    # A revision-less poll is an asynchronous wait, not a consumed reasoning
    # turn.  Keep a finite guard for a genuinely dead compatibility backend,
    # while allowing a just-started native Worker enough time to publish its
    # next Blackboard event.
    max_idle_polls: int = 30
    # Stop-convergence window.  A stop request (Run deadline, operator cancel)
    # used to abandon the in-flight Worker round the instant it was observed:
    # the loop broke, Finalize swept the Board, and whatever that round was
    # about to verify was reaped instead of recorded.  That is exactly the
    # "direction stopped before its verification completed" reading.  With a
    # non-zero budget the Coordinator keeps the *current* round alive for up to
    # this many seconds before Finalize runs -- and stops early the moment the
    # pool has no active Worker, so a Run whose Workers already finished pays
    # nothing.  The wait is conditional, not a fixed sleep: its real duration
    # is decided by when the in-flight verification lands.
    #
    # This budget must stay below the Runtime's own Finalize grace
    # (``muteki_runtime.FINALIZE_GRACE_SECONDS``): Finalize still has to cancel
    # the Worker, reap the Sandbox and publish the terminal event inside that
    # bound.  ``stop_convergence_seconds=0`` restores the old behaviour.
    stop_convergence_seconds: float = 120.0
    # Absolute monotonic deadline for the whole Run, or ``None`` when the caller
    # owns no deadline (focused tests, bounded compatibility callers).
    #
    # A Worker round can only be *verified* if it gets to finish: a round that is
    # still running when the deadline lands is reaped, and every direction it was
    # verifying is closed as ``timed_out`` with no result -- precisely the
    # "direction stopped before its verification completed" reading the
    # convergence window exists to prevent, arriving through the front door.
    #
    # The convergence window cannot fix that on its own: it is bounded well below
    # ``worker_timeout_seconds`` (it must stay under the Runtime's Finalize
    # grace), so it can only capture a round that is nearly done.  Knowing the
    # deadline lets the Coordinator decline to *start* a round that cannot
    # finish, which is what actually makes "verification completed" reachable.
    run_deadline_monotonic: float | None = None
    # The first move of the curated recipe for this challenge, lifted out of it
    # by the Runtime and injected into the exploit lane's goal.
    #
    # Measured on ``[Solr]CVE-2019-0193`` (run ``50a60596``): the recipe was in
    # ``AGENTS.md``, the Worker read it, and the round still spent 71 requests on
    # the Solr config API against 2 on the endpoint the recipe names.  Reading
    # the file is an instruction the Worker can satisfy and then ignore; the goal
    # is the one place it cannot treat as background reading.  Empty means "no
    # recipe applies" and leaves the lane byte-identical to before.
    exploit_first_action: str = ""

    # Where ``exploit_first_action`` came from: the recipe entry id plus the field
    # the text was read from, and the ``_fa_is_move`` verdict that admitted it.
    #
    # Only used for the ``exploit.first_action`` telemetry event.  The slot is the
    # highest-authority line in the prompt, so a silent rewrite of it is exactly
    # the kind of change that cannot be audited six weeks later; carrying the
    # provenance lets the event be joined against Run outcomes.
    exploit_first_action_source: str = ""
    exploit_first_action_verdict: str = ""

    # The first move of a recipe that MATCHED this box but was never measured on
    # it.  Rendered as a probe the Worker is told to verify, never as an order --
    # which is why it is a separate field from ``exploit_first_action`` rather
    # than a loosening of its trust gate.
    #
    # Measured 2026-11-08 over the 81 UNSOLVED arena Challenges: only **1** could
    # issue a FIRST ACTION.  The other 80 fell back to ``technique_candidate_goal``,
    # and 68 of them shared the *same* string (``cmdi=param→shell; delay or echo;
    # SSTI=arith in param evaluated``) despite 35 naming a specific CVE in the
    # title and 21 already matching a real vulhub recipe -- 13 of which contained
    # an executable first line (``POST /service/extdirect``, ``GET
    # /static/../../../../etc/passwd``).  Those lines were being discarded while
    # the Worker was told to guess a technique class.
    #
    # The gate that discarded them is correct in what it protects (the S2-009
    # recipe passed every schema check and was wrong for the instance), so the fix
    # is a lower-authority channel, not a weaker gate.
    exploit_probe_action: str = ""

    # A one-line feasibility question prepended to the exploit goal, for a
    # recipe whose preconditions name a callback channel.
    #
    # Measured twice on ``[Weblogic]CVE-2018-2628`` (runs ``4b5ceb4c`` and
    # ``90b58afe``): both rounds spent the full 14-15 minute budget and the
    # second one had already read ``AGENTS.md``, which says in prose that the
    # JRMP chain needs the target to call back.  Prose the Worker can skim is
    # not a gate; a question it has to answer before acting is.  Empty means
    # "this recipe needs no callback" and leaves the lane byte-identical.
    exploit_stop_loss_gate: str = ""

    # The "no recipe: classify first" clause, for a box whose title carries no
    # CVE id and no advisory number.
    #
    # ``exploit_first_action`` above only speaks when a recipe matched, and the
    # recipe index is keyed on identifiers.  A sampled 30 of the arena's ordinary
    # Web boxes carried zero CVE ids and zero advisory numbers -- only an event
    # name and a title -- so they reached the exploit lane with the bare template
    # and started from nothing.  This clause names the candidate technique classes
    # and the observation that confirms each, which is what those boxes need.
    # Empty when a recipe did match: a recipe is strictly more specific.
    technique_candidate_goal: str = ""


class MutekiCoordinator:
    """Four-phase Muteki coordinator over one canonical graph."""

    # Class-level defaults for the convergence state.  Focused tests build the
    # Coordinator with ``object.__new__`` and set only the fields the code path
    # under test reads; Finalize reads convergence on every path, so these must
    # answer before ``__init__`` has run.
    _converging: bool = False
    _stop_requested: bool = False
    #: Commands seen since the last new fact/dead-end, and the lock guarding it.
    #: Class-level for the same reason as the flags above: tests (and the
    #: Worker-thread path) can reach ``_note_command_for_stagnation`` before
    #: ``__init__`` has run, and an AttributeError there would take down the
    #: whole step callback.
    _commands_since_progress: int = 0
    #: True once a ``DEADEND=`` marker has been seen in this Run.  Shrinks the
    #: stagnation budget from ``stagnation_command_limit`` to
    #: ``stagnation_after_deadend_limit``.
    _deadend_seen: bool = False
    #: ``threading.Lock`` cannot be a plain class attribute (it would be shared
    #: across instances), so it is created lazily in ``_stagnation_lock``.
    _stagnation_lock_ref: Any = None
    #: True once a Worker round was abandoned because *its* deadline expired.
    #:
    #: Distinct from ``_stop_requested`` on purpose: a stop is an operator
    #: withdrawing the Run, a timeout is our own clock expiring while work was
    #: still in flight.  Only the latter may still re-read evidence and salvage
    #: a flag, and only the latter should close the Run as ``TIMEOUT``.
    _timed_out: bool = False
    _converge_reason: str | None = None
    _converge_deadline: float | None = None
    _converge_started: float | None = None
    _converged_rounds: int = 0
    _converge_wakeup: asyncio.Event | None = None
    _stop_wakeup: asyncio.Event | None = None

    def _convergence_budget_seconds(self) -> float:
        """Return this Run's stop-convergence allowance, clamped to the cap.

        Read defensively: a Coordinator constructed without ``__init__`` (test
        doubles) has no config, and the old behaviour -- no convergence window
        -- is the correct fallback there.
        """

        config = getattr(self, "config", None)
        raw = getattr(config, "stop_convergence_seconds", 0.0) if config is not None else 0.0
        try:
            value = float(raw or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        return min(max(0.0, value), STOP_CONVERGENCE_MAX_SECONDS)

    def _seconds_left(self) -> float | None:
        """Seconds until the Run deadline, or ``None`` when there is no deadline.

        Read defensively: a Coordinator built without ``__init__`` (test doubles)
        has no config, and "no deadline" is the correct answer there -- it
        restores the pre-existing behaviour of dispatching unconditionally.
        """

        config = getattr(self, "config", None)
        deadline = getattr(config, "run_deadline_monotonic", None) if config is not None else None
        if deadline is None:
            return None
        try:
            return float(deadline) - time.monotonic()
        except (TypeError, ValueError):
            return None

    def _round_budget_seconds(self) -> float:
        """The wall-clock budget one Worker round is granted, plus its grace."""

        config = getattr(self, "config", None)
        raw_timeout = getattr(config, "worker_timeout_seconds", 0) if config is not None else 0
        raw_grace = getattr(config, "worker_wait_grace_seconds", 0.0) if config is not None else 0.0
        try:
            timeout = float(raw_timeout or 0)
        except (TypeError, ValueError):
            timeout = 0.0
        try:
            grace = float(raw_grace or 0.0)
        except (TypeError, ValueError):
            grace = 0.0
        return max(0.0, timeout) + max(0.0, grace)

    def _a_round_still_fits(self) -> bool:
        """Can a Worker round started *now* still finish before the deadline?

        A round that cannot finish is not merely wasted work: the deadline reaps
        it, so every direction it was verifying is closed ``timed_out`` with no
        result.  Starting it converts "verification completed" into "verification
        abandoned" and spends a full round's tokens to do it, so the round is
        declined instead.  The in-flight rounds (started when there was time) are
        untouched and still get to land their results.
        """

        left = self._seconds_left()
        if left is None:
            return True
        return left >= self._round_budget_seconds()

    def _reserve_the_last_round(self) -> None:
        """Hand the rest of the budget to the rounds already running.

        Called when a round is declined for lack of time.  Convergence is the
        established way to say "stop opening directions, finish what is running":
        it lets the loop drain the pool and exit through the normal path instead
        of spinning until ``max_idle_polls`` trips ``NO_GRAPH_PROGRESS``.
        """

        if self._stop_requested or self._converging:
            return
        left = self._seconds_left()
        self.request_convergence(
            "RUN_BUDGET_RESERVED_FOR_LAST_ROUND",
            None if left is None else max(0.0, left),
        )

    def _flag_pattern(self) -> str:
        """Return the challenge flag shape every Worker must look for.

        The graph owns it because both the compatibility graph and the official
        SharedGraph facade are seeded from the same Challenge.  Hardcoding the
        pattern here silently rejected challenges whose banner is not ``flag``.
        """

        return str(getattr(self.graph, "flag_pattern", "") or "") or DEFAULT_FLAG_PATTERN

    def __init__(
        self,
        graph: MutekiGraph,
        reason: MutekiReason,
        pool: MutekiWorkerPool,
        engines: list[EngineProfile],
        *,
        config: CoordinatorConfig | dict | None = None,
        cli_driver: "CLIDriver | None" = None,
        container_executor: ContainerExecutor | None = None,
        control_receiver: ControlReceiver | None = None,
        official_worker_adapter: OfficialWorkerAdapter | None = None,
        official_worker_evidence_bridge=None,
        official_worker_usage_bridge=None,
        insight_bus=None,
        evidence_reader=None,
    ) -> None:
        self.graph = graph
        self.reason = reason
        self.pool = pool
        self.engines = engines
        #: Optional callable returning the retained tool output of this Run.
        #:
        #: Needed because the Muteki graph does not hold it: the ``upstream``
        #: backend records structure (lanes, intents, facts) while the tool
        #: text lands in the platform's ``run_events`` projection as
        #: ``result_excerpt``.  A recovery of "the value the target printed" has
        #: to read that, and the Coordinator must not import the database to do
        #: it -- so the Runtime injects the reader and tests pass one directly.
        self._evidence_reader = evidence_reader
        self.config = CoordinatorConfig(**config) if isinstance(config, dict) else (config or CoordinatorConfig(max_workers=pool.max_workers))
        self.stage_policy = StagePolicy.from_config(self.config.stage_policy)
        if self.config.worker_backend not in {
            "callback",
            "cli",
            "container",
            "upstream_local",
            "upstream_container",
        }:
            raise ValueError(f"unsupported Muteki worker backend: {self.config.worker_backend}")
        if (
            self.config.worker_backend in {"upstream_local", "upstream_container"}
            and not callable(getattr(graph, "native_graph", None))
        ):
            raise ValueError(
                "upstream native Worker/Sandbox requires "
                "APP_MUTEKI_GRAPH_BACKEND=upstream"
            )
        if self.config.worker_backend == "cli" and cli_driver is None:
            from .cli_driver import CLIDriver

            cli_driver = CLIDriver(timeout_seconds=self.config.worker_timeout_seconds)
        self.cli_driver = cli_driver
        self.container_executor = container_executor or (ContainerExecutor(timeout_seconds=self.config.worker_timeout_seconds) if self.config.worker_backend == "container" else None)
        self.control_receiver = control_receiver
        self.official_worker: OfficialWorkerAdapter | None = official_worker_adapter
        self.official_worker_usage_bridge = official_worker_usage_bridge
        self.insight_bus = insight_bus
        if self.config.worker_backend in {"upstream_local", "upstream_container"}:
            if self.official_worker is None:
                native_graph = None
                native_graph_factory = getattr(graph, "native_graph", None)
                if callable(native_graph_factory):
                    native_graph = native_graph_factory()
                self.official_worker = OfficialWorkerAdapter(
                    OfficialWorkerConfig(
                        backend="container"
                        if self.config.worker_backend == "upstream_container"
                        else "local",
                        timeout_seconds=self.config.worker_timeout_seconds,
                        max_turns=max(1, int(self.config.worker_max_turns)),
                        # Keep the native Worker on the same explicitly
                        # authorized Docker network as the existing container
                        # execution path.  The default remains bridge for
                        # callers that do not opt into a target network.
                        network=os.environ.get("MUTEKI_CONTAINER_NETWORK", "bridge"),
                        protocol="cli_solver",
                    ),
                    step_callback=self._record_worker_step,
                    evidence_bridge=official_worker_evidence_bridge,
                    usage_bridge=official_worker_usage_bridge,
                    shared_graph=native_graph,
                )
            self.pool.external_runner = self._official_worker_runner
            # The official native Worker/Sandbox is the complete execution
            # boundary.  Do not let selected local-model profiles or a
            # compatibility callback fall through to ToolGateway/Kali Runner.
            self.pool.external_runner_exclusive = True
        if self.pool.review_handler is None:
            self.pool.review_handler = self._review_runner
        if self.config.worker_backend in {"cli", "container"}:
            self.pool.external_runner = self._external_worker_runner
        self.phase = MutekiPhase.PREPARE
        self._worker_number = 0
        # Per-Worker coalescing state for WORKER_STEP observability.  Worker
        # callbacks arrive from the Worker's own thread, so the map is guarded by
        # its own lock rather than the graph lock.  Entries are per-Worker and
        # removed when the Worker's run returns.
        self._worker_step_state: dict[str, dict[str, Any]] = {}
        self._worker_step_lock = threading.Lock()
        self._last_revision = -1
        self._finalized = False
        # In-progress guard for Finalize.  ``_finalized`` means "the terminal
        # RUN_FINISHED event was published"; keeping the two separate is what
        # stops a cancelled Finalize from leaving the Run marked terminal while
        # the event is still missing.
        self._finalizing = False
        self._stop_requested = False
        # Convergence is a *softer* boundary than stop: the Run is out of time
        # for new directions but the in-flight Worker round is still allowed to
        # finish.  ``request_convergence`` raises it; the poll loop then stops
        # dispatching and only waits for the pool to drain.
        self._converging = False
        self._converge_reason: str | None = None
        # Absolute monotonic deadline for the shared convergence allowance.
        # ``None`` means "no convergence was ever requested"; Finalize then
        # grants the budget itself for stop paths that skipped the loop.
        self._converge_deadline: float | None = None
        # ``request_stop`` is a synchronous boundary (Run-level deadline or an
        # operator cancel).  Publishing it as a wake-up event lets the poll
        # sleep and the Worker-round wait abort in seconds instead of running
        # out the whole Worker window before Finalize becomes reachable.  The
        # event is created in ``run`` so the Coordinator stays loop-affine.
        self._stop_wakeup: asyncio.Event | None = None
        # Separate door for the softer convergence boundary, so a convergence
        # signal can interrupt the poll sleep without pre-arming the hard-stop
        # wait inside the convergence branch.
        self._converge_wakeup: asyncio.Event | None = None
        self._dispatched_intents: set[str] = set()
        self._semantic_reservations: dict[str, SemanticReservation] = {}
        self._active_route_hashes: set[str] = set()
        self._bootstrap_workers = 0
        self._spawned_workers = 0
        self._budget_exhausted = False
        self._stop_reason: str | None = None
        self._unavailable_engine_ids: set[str] = set()
        # A failed Prepare probe is a temporary availability state.  Keep the
        # engine out of the current dispatch round, but re-probe it in the
        # background after Worker rounds instead of permanently skipping it.
        self._health_retry_task: asyncio.Task[None] | None = None
        self._health_retry_attempts: dict[str, int] = {}
        self._reason_health_retry_task: asyncio.Task[None] | None = None
        self._reason_health_retry_attempts = 0
        self._engine_cursor = 0
        self._barren_workers = 0
        # Command-level stagnation, distinct from the round-level counter above.
        self._commands_since_progress = 0
        self._deadend_seen = False
        # Consecutive rounds whose only "progress" was a Worker's own
        # unverified self-report.  Bounded, because a self-report is a clue and
        # must not keep resetting the barren counter forever.
        self._unverified_only_rounds = 0
        self._last_progress_t = 0.0

    @property
    def stop_reason(self) -> str | None:
        """Return a controlled stop reason produced during this Run."""

        return self._stop_reason

    async def run(self, *, max_ticks: int | None = None, unbounded: bool = False) -> None:
        limit = None if unbounded else self.config.max_ticks if max_ticks is None else max(0, max_ticks)
        # A stop request can arrive at any point of one poll (a Run-level
        # deadline fires mid Worker-turn, an operator cancels mid sleep).
        # Without a wake-up the loop only observes it at the next tick
        # boundary, which can be a whole Worker timeout away.
        self._stop_wakeup = asyncio.Event()
        self._converge_wakeup = asyncio.Event()
        if self._stop_requested:
            self._stop_wakeup.set()
        if self._converging:
            self._converge_wakeup.set()
        try:
            await self._prepare()
            if self.config.race_enabled:
                await self._race()
            if self._stop_requested or self.graph.flags(verified_only=True):
                return
            self._change_phase(MutekiPhase.COORDINATOR)
            tick = 0
            idle_polls = 0
            while limit is None or tick < limit:
                if self._stop_requested or self.graph.flags(verified_only=True):
                    break
                if self._converging:
                    # Soft deadline reached.  Opening another direction would
                    # only be reaped by Finalize, so the loop stops deciding --
                    # but the round already in flight still owns its remaining
                    # time, and that round is where a pending verification
                    # actually lands.  Leave the loop once the pool drains.
                    if not self.pool.active_count:
                        break
                    completion = await self._await_convergence_round()
                    if completion is None:
                        # Hard stop arrived, or the window is spent: hand over
                        # to Finalize instead of waiting out the round.
                        break
                    if completion:
                        self._converged_rounds = getattr(self, "_converged_rounds", 0) + 1
                    else:
                        # The round hit its own Worker timeout; recovery owns
                        # the reap and the fence against a hung round.
                        await self._recover_timed_out_workers()
                    continue
                revision = self.graph.revision()
                if revision == self._last_revision:
                    # A revision-less poll is not an OODA turn.  The official
                    # Muteki Coordinator waits for an event/Graph change and
                    # only then asks Reason for another Intent.  Consuming the
                    # finite turn budget here was the source of premature
                    # STOPPED runs when a container Worker was still starting.
                    if (
                        not self.pool.active_count
                        and not self.graph.flags(verified_only=True)
                    ):
                        # An open Intent is already a Coordinator decision.
                        # Consume it before the whole-challenge recovery path;
                        # upstream Muteki never replaces a claimable route with
                        # a fresh bootstrap merely because the poll was idle.
                        before_open = self.pool.active_count
                        await self._dispatch_open_intent()
                        if self.pool.active_count > before_open:
                            await self._wait_for_worker_round()
                            tick += 1
                            idle_polls = 0
                            continue
                        before_bootstrap = self._bootstrap_workers
                        await self._dispatch_bootstrap()
                        if self._bootstrap_workers > before_bootstrap:
                            # A bootstrap is a real Worker round even when the
                            # native Worker only reports a dead-end and the
                            # facade has not observed a new upstream event yet.
                            # Wait for that round before charging one safety
                            # tick; otherwise an unlimited bootstrap policy can
                            # repeatedly spawn from this idle branch forever.
                            await self._wait_for_worker_round()
                            tick += 1
                            idle_polls = 0
                            continue
                    # If all currently selected engines are unavailable, keep
                    # the reactivation probe alive even though no Worker round
                    # can be dispatched yet.  This is local to this Run and
                    # never blocks another Run's Coordinator.
                    self._schedule_health_retry()
                    self._schedule_reason_health_retry()
                    idle_polls += 1
                    if idle_polls >= max(1, int(self.config.max_idle_polls)):
                        self.graph.add_dead_end(
                            actor="coordinator",
                            description="NO_GRAPH_PROGRESS",
                        )
                        break
                    await self._interruptible_sleep(max(0.01, self.config.interval_seconds))
                    continue
                idle_polls = 0
                tick += 1
                # A Reason pass is only worth spending when somebody can act on
                # its output.  Measured on run ``780fc60b`` (Weblogic
                # CVE-2018-2628): the Worker turn timed out at 03:04:23, the
                # Coordinator still issued a full Reason call, and it returned
                # at 03:05:17 -- 54 seconds of planning whose Intents no Worker
                # could ever execute, because ``_a_round_still_fits()`` was
                # already false.  The Run then had to claim and immediately
                # conclude that unexecutable Intent, which is exactly the
                # "repeated processing / inconsistent state" the timeout path is
                # supposed to avoid.  Stop deciding instead: Finalize already
                # owns claim-then-conclude for every direction it inherits.
                if (
                    self.pool.active_count == 0
                    and not self.graph.flags(verified_only=True)
                    and not self._a_round_still_fits()
                ):
                    self.graph.add_dead_end(
                        actor="coordinator",
                        description="ROUND_BUDGET_EXHAUSTED_BEFORE_REASON",
                    )
                    break
                # Consume this snapshot before dispatching.  Worker facts
                # written during this turn must remain a new revision for the
                # following turn; assigning the post-worker revision here
                # would skip the next Reason pass entirely.
                self._last_revision = revision
                self.graph.emit_event(
                    actor="coordinator",
                    event_type=EventType.REASON_STARTED,
                    payload={
                        "phase": self.phase.value,
                        "revision": revision,
                        "active_workers": self.pool.active_count,
                        **self._reason_provider_telemetry(),
                    },
                )
                try:
                    result = await self.reason.reason(self.graph)
                except Exception as error:
                    # A Reason provider outage must remain observable and must
                    # not make the Coordinator disappear before it can choose
                    # a recovery/bootstrap turn.
                    result = ReasonResult(
                        False,
                        (),
                        verdict="course_correct",
                        drift="REASON_PROVIDER_FAILED",
                    )
                    self.graph.emit_event(
                        actor="coordinator",
                        event_type=EventType.REASON_FAILED,
                        payload={
                            "phase": self.phase.value,
                            "revision": revision,
                            "reason_code": type(error).__name__.upper()[:80],
                        },
                    )
                else:
                    self.graph.emit_event(
                        actor="coordinator",
                        event_type=EventType.REASON_COMPLETED,
                        payload={
                            "phase": self.phase.value,
                            "revision": revision,
                            "verdict": str(result.verdict),
                            "goal_met": bool(result.goal_met),
                            "intent_count": len(result.intents),
                            **self._reason_provider_telemetry(),
                        },
                    )
                self.reason.write_intents(self.graph, result)
                await self._dispatch_open_intent()
                # Official Muteki treats ``course_correct`` as a scheduling
                # decision. Reuse the existing Review Worker and StagePolicy
                # so the next branch is selected from the canonical Graph.
                # Do not copy the model's drift prose into audit or Worker
                # payloads; the Review Worker reads the Graph directly.
                if result.verdict == "course_correct":
                    self.graph.emit_event(
                        actor="coordinator",
                        event_type=EventType.COORDINATOR_DIRECTIVE,
                        payload={"directive": "course_correct", "review_requested": True},
                    )
                    await self._dispatch_review(goal="review course-corrected route")
                if (
                    not result.intents
                    and not self.pool.active_count
                    and not self.graph.flags(verified_only=True)
                ):
                    await self._dispatch_bootstrap()
                await self._wait_for_worker_round()
                if (
                    self.config.barren_limit > 0
                    and self._barren_workers >= self.config.barren_limit
                ):
                    self._stop_requested = True
                    self._stop_reason = f"BARREN_LIMIT:{self._barren_workers}"
                    self.graph.add_dead_end(
                        actor="coordinator",
                        description=f"BARREN_LIMIT {self._barren_workers} fruitless Worker rounds",
                    )
                    return
                if self.config.review_interval > 0 and tick % self.config.review_interval == 0:
                    await self._dispatch_review()
                await asyncio.sleep(0)
                if self.config.interval_seconds:
                    await self._interruptible_sleep(self.config.interval_seconds)
        finally:
            # The terminal reason must say *why* the loop ended, because the
            # Orchestrator maps it straight onto the Run's status.  Emitting a
            # bare ``STOPPED`` here discarded that: measured on the real-class
            # Runs (``0ab61266`` / ``879d42b0`` / ``87e96c2e``), each one recorded
            # ``RACE_WORKER_TIMEOUT`` as a dead end and then closed with
            # ``reason: STOPPED``, so ``MutekiOrchestrator.run`` never took its
            # TIMEOUT branch and all three landed in COMPLETED_UNSOLVED.
            #
            # ``_stop_reason`` is only set by paths that decided to stop
            # (barren limit, worker budget, an operator stop), so it is the
            # right source when present; otherwise a timed-out round is what
            # ended us and that is what the record should say.
            terminal_reason = "SOLVED" if self.graph.flags(verified_only=True) else (
                self._stop_reason or (TIMED_OUT_STOP_REASON if self._timed_out else "STOPPED")
            )
            await self.finalize(reason=terminal_reason)

    async def _wait_for_worker_round(self) -> None:
        """Wait for one Worker round and schedule engine reactivation.

        Engine health recovery is deliberately fire-and-forget within this
        Run.  The next Worker round is allowed to continue while the failed
        engine is probed; the probe itself waits until no Worker is active so
        it cannot compete with an execution window in the same Sandbox.
        """

        if not self.pool.active_count:
            self._schedule_health_retry()
            self._schedule_reason_health_retry()
            return
        fact_count_before = len(self.graph.facts())
        flag_count_before = len(self.graph.flags(verified_only=True))
        try:
            round_finished = await self._await_worker_round(
                timeout=max(
                    1.0,
                    float(self.config.worker_timeout_seconds)
                    + max(0.0, float(self.config.worker_wait_grace_seconds)),
                )
            )
        except asyncio.TimeoutError:
            self.graph.add_dead_end(actor="coordinator", description="WORKER_TIMEOUT")
            await self._recover_timed_out_workers()
        else:
            if not round_finished:
                # A stop request interrupted the round.  Finalize owns the
                # Worker reaping, and this poll must not be charged as a barren
                # Worker round (that would overwrite the stop reason
                # downstream with BARREN_LIMIT).
                return
        # Track progress: a Worker round that publishes a new verified fact or
        # verified flag counts as progress.  REVIEW_NEEDED / dead-end markers
        # from the review worker are NOT real progress.
        new_facts = self.graph.facts()
        new_flags = self.graph.flags(verified_only=True)
        added = new_facts[fact_count_before:]
        # The stagnation allowance is a function of the fact count, and this is
        # the one place it can change.  Dropping the memo here (rather than
        # inside `_stagnation_limit_for_progress`) keeps the lookup off the
        # per-command hot path -- it runs once per Worker command from another
        # thread, where a SQLite read per call would be pure overhead.
        self.__dict__.pop("_stagnation_fact_count", None)
        # ── end-of-cycle verification gate ────────────────────────────────
        # "The fact count grew" used to be the whole definition of progress, so
        # a Worker repeating its own unverified self-report could keep the Run
        # alive forever (and, worse, feed the next cycle a premise nobody had
        # checked).  Verified output -- a verified flag or a host-verified fact
        # -- is real progress; a self-report is progress for a bounded number of
        # rounds only, after which the round counts as barren.
        flag_progress = len(new_flags) > flag_count_before
        verified_progress = any(bool(getattr(fact, "verified", False)) for fact in added)
        novel = any(
            not str(getattr(fact, "content", "")).startswith("REVIEW_NEEDED")
            for fact in added
        )
        if flag_progress or verified_progress:
            self._unverified_only_rounds = 0
            made_progress = True
        elif novel:
            self._unverified_only_rounds += 1
            made_progress = self._unverified_only_rounds <= UNVERIFIED_ONLY_ROUNDS_LIMIT
        else:
            made_progress = False
        self._close_cycle_semantics(added)
        # Harvest flag-shaped values *while the Run is still running*, not only
        # at Finalize.  A box that prints its own banner (``n1book{...}`` on the
        # real-class targets) would otherwise keep its answer invisible until the
        # clock expired, and the next Reason turn could not see it either.
        # Recording it now costs one write per new value and makes the candidate
        # part of the board the loops read.
        await self._harvest_candidates()
        if made_progress:
            self._barren_workers = 0
            self._last_progress_t = time.monotonic()
        else:
            self._barren_workers += 1
        self._release_completed_semantics()
        self._schedule_health_retry()
        self._schedule_reason_health_retry()

    def _close_cycle_semantics(self, added_facts: list[Any]) -> None:
        """Close one cycle: challenge unverified facts, conclude unclaimed intents.

        Nothing unverified and nothing unowned is allowed to cross into the next
        cycle.  Concretely:

        * every fact a Worker added about its **own** work is published as
          ``FACT_CHALLENGED``.  It stays on the Blackboard as a lead; what
          changes is that the next cycle may no longer treat it as an
          established premise.  The phpMyAdmin run is the cautionary case: its
          "achieved RCE, PWN42 executed" fact carried ``confidence=0.4`` and a
          self-``verifier``, and it steered the rest of the round.
        * an Intent that is still ``open`` after the round is claimed by the
          Coordinator and concluded, exactly like Finalize does.  An open Intent
          nobody picked up is a direction with no Worker behind it; leaving it
          open makes the next cycle inherit it and re-propose the same route.
        """

        unverified = [
            fact for fact in added_facts if not bool(getattr(fact, "verified", False))
        ]
        emit_event = getattr(self.graph, "emit_event", None)
        if unverified and callable(emit_event):
            try:
                emit_event(
                    actor="coordinator",
                    event_type=EventType.FACT_CHALLENGED,
                    payload={
                        "count": len(unverified),
                        "fact_ids": [
                            str(getattr(fact, "fact_id", "") or "")[:36]
                            for fact in unverified[:20]
                        ],
                        "reason": "UNVERIFIED_SELF_REPORT",
                        "rule": "unverified facts are leads, not premises",
                        "unverified_only_rounds": int(self._unverified_only_rounds),
                    },
                )
            except Exception:
                pass
        self._conclude_unclaimed_intents()

    def _conclude_unclaimed_intents(self) -> None:
        """Claim-then-conclude every Intent no Worker picked up this cycle."""

        intents = getattr(self.graph, "intents", None)
        claim = getattr(self.graph, "claim_intent", None)
        conclude = getattr(self.graph, "conclude_intent", None)
        if not (callable(intents) and callable(claim) and callable(conclude)):
            return
        try:
            open_intents = list(intents(status="open") or [])
        except Exception:
            return
        for intent in open_intents:
            intent_id = str(getattr(intent, "intent_id", "") or "")
            if not intent_id:
                continue
            try:
                claimed = claim(
                    worker="coordinator",
                    intent_id=intent_id,
                    lease_s=FINALIZE_INTENT_LEASE_SECONDS,
                )
            except Exception:
                continue
            if not claimed:
                # A Worker won the race and owns it now: never steal a lease.
                continue
            try:
                conclude(
                    actor="coordinator",
                    intent_id=intent_id,
                    result="unclaimed_at_cycle_end",
                )
            except Exception:
                continue

    async def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep for the poll interval, returning early on a stop request.

        Pacing is unchanged when the Run is healthy: without a stop request
        this waits exactly ``seconds``.  It only shortens the wait when
        ``request_stop`` has been observed.
        """

        if seconds <= 0 or self._stop_requested:
            await asyncio.sleep(0)
            return
        # Both boundaries shorten this wait: a hard stop ends the Run, and a
        # convergence request must reach the top of the loop promptly so the
        # poll stops dispatching while the current round is still running.
        wakeups = tuple(
            event
            for event in (self._stop_wakeup, self._converge_wakeup)
            if event is not None
        )
        if not wakeups:
            await asyncio.sleep(seconds)
            return
        waiters = [asyncio.ensure_future(event.wait()) for event in wakeups]
        try:
            await asyncio.wait(
                waiters,
                timeout=seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for waiter in waiters:
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

    async def _await_worker_round(self, *, timeout: float, stop_on_flag: bool = False) -> bool:
        """Wait for one Worker round, interruptible by a stop request.

        Returns ``True`` when the round completed, ``False`` when a stop
        request ended the wait early, and raises ``asyncio.TimeoutError`` when
        the Worker window itself expired.  The pool barrier is only a waiter:
        abandoning it never cancels the Worker task, so Finalize still owns
        the official cancel/reap ordering.

        ``stop_on_flag`` makes a verified flag an *end of round* condition.
        The Race exists to produce one gate-verified flag, not to run every
        lane to completion: lanes still probing cannot make an already
        verified flag less true, and waiting for the slowest one is what spent
        a whole Worker budget after the answer was already on the Board.
        Returning early is safe because the abandoned barrier only stops
        *waiting* -- the lanes keep running until Finalize reaps them.
        """

        wakeup = self._stop_wakeup
        if wakeup is None and not stop_on_flag:
            await asyncio.wait_for(self.pool.wait(), timeout=timeout)
            return True
        round_waiter = asyncio.ensure_future(self.pool.wait())
        stop_waiter = asyncio.ensure_future(wakeup.wait()) if wakeup is not None else None
        flag_waiter = (
            asyncio.ensure_future(self._await_verified_flag())
            if stop_on_flag
            else None
        )
        waiters = tuple(
            waiter
            for waiter in (round_waiter, stop_waiter, flag_waiter)
            if waiter is not None
        )
        try:
            done, _ = await asyncio.wait(
                waiters,
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if round_waiter in done:
                round_waiter.result()
                return True
            if flag_waiter is not None and flag_waiter in done:
                flag_waiter.result()
                return True
            if stop_waiter is not None and stop_waiter in done:
                return False
            raise asyncio.TimeoutError
        finally:
            for waiter in waiters:
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

    async def _await_verified_flag(self, interval: float = 1.0) -> None:
        """Return as soon as the Blackboard holds a gate-verified flag.

        Polling rather than an event subscription keeps this independent of
        whichever graph backend is in use; the interval only bounds how long
        a finished Race can lag behind the flag it was waiting for.
        """

        while not self.graph.flags(verified_only=True):
            await asyncio.sleep(max(0.05, float(interval)))

    async def _await_convergence_round(self) -> bool | None:
        """Let the in-flight Worker round finish under a soft deadline.

        The round keeps the *native* Worker budget: convergence does not shorten
        it, because a verification that is nearly done is exactly what the
        window exists to capture.  Only a hard stop ends the wait early.

        Returns ``True`` when the round completed, ``False`` when the round
        itself timed out, and ``None`` when the caller must hand over to
        Finalize (a hard stop arrived, or the shared convergence allowance ran
        out).
        """

        deadline = self._converge_deadline
        if deadline is None:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        # The round keeps its own budget; convergence only caps the *total*
        # time the Run is willing to spend waiting for it.
        timeout = min(
            remaining,
            max(
                1.0,
                float(self.config.worker_timeout_seconds)
                + max(0.0, float(self.config.worker_wait_grace_seconds)),
            ),
        )
        round_waiter = asyncio.ensure_future(self.pool.wait())
        waiters: list[asyncio.Future] = [round_waiter]
        stop_waiter: asyncio.Future | None = None
        if self._stop_wakeup is not None:
            stop_waiter = asyncio.ensure_future(self._stop_wakeup.wait())
            waiters.append(stop_waiter)
        try:
            done, _ = await asyncio.wait(
                waiters,
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            # A hard stop outranks a round that finished in the same tick: the
            # Run deadline has already passed and Finalize owns the handover.
            if stop_waiter is not None and stop_waiter in done:
                return None
            if round_waiter in done:
                round_waiter.result()
                return True
            # No completion: distinguish "the round is still running past its
            # own Worker budget" from "the Run has spent its convergence
            # allowance".  Only the former is a Worker timeout to recover.
            return False if time.monotonic() < deadline else None
        finally:
            for waiter in waiters:
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

    def _schedule_reason_health_retry(self) -> None:
        """Probe an unavailable Coordinator Reason model asynchronously."""

        provider = getattr(self.reason, "provider", None)
        if (
            self._finalized
            or provider is None
            or not bool(getattr(provider, "needs_reactivation", False))
            or (
                self._reason_health_retry_task is not None
                and not self._reason_health_retry_task.done()
            )
        ):
            return
        health = getattr(provider, "health", None)
        if not callable(health):
            return
        self._reason_health_retry_task = asyncio.create_task(
            self._retry_reason_model(provider, health),
            name=f"muteki-reason-health-retry-{self.graph.challenge_id}",
        )

    async def _retry_reason_model(self, provider, health) -> None:
        """Re-probe Reason without blocking Worker dispatch or other Runs."""

        self._reason_health_retry_attempts += 1
        attempt = self._reason_health_retry_attempts
        try:
            healthy, reason_code = await health()
        except Exception:
            healthy, reason_code = False, "REASON_HEALTHCHECK_FAILED"
        payload = {
            "attempt": attempt,
            "healthy": bool(healthy),
            "reason_code": _safe_worker_reason(reason_code)
            or ("HEALTHY" if healthy else "REASON_HEALTHCHECK_FAILED"),
            "reactivated": bool(healthy),
            "source": "coordinator_reason_model",
        }
        error_detail = str(getattr(provider, "last_health_error_detail", "") or "")[:200]
        if error_detail:
            payload["error_detail"] = error_detail
        self.graph.emit_event(
            actor="coordinator",
            event_type=(
                EventType.REASON_MODEL_REACTIVATED
                if healthy
                else EventType.REASON_MODEL_FALLBACK
            ),
            payload=payload,
        )

    def _schedule_health_retry(self) -> None:
        """Schedule one non-blocking reactivation probe for this Run."""

        if (
            self._finalized
            or self.official_worker is None
            or not self._unavailable_engine_ids
            or (
                self._health_retry_task is not None
                and not self._health_retry_task.done()
            )
        ):
            return
        self._health_retry_task = asyncio.create_task(
            self._retry_unavailable_engines(),
            name=f"muteki-health-retry-{self.graph.challenge_id}",
        )

    async def _retry_unavailable_engines(self) -> None:
        """Re-probe failed engines and return recovered profiles to dispatch."""

        health = getattr(self.official_worker, "health", None)
        chat_retry = [
            profile
            for profile in self.engines
            if profile.engine_id in self._unavailable_engine_ids
            and str((profile.driver_profile or {}).get("protocol") or "")
            == "chat_completions"
        ]
        if not callable(health) and not chat_retry:
            return
        # Do not run a health process against the same Sandbox while another
        # Worker is active.  The task remains independent from the Worker and
        # resumes as soon as the current round has released the Sandbox.
        #
        # Bounded, because "wait for idle" turned into "wait forever" whenever
        # the surviving lane kept producing work: run ``4666646b`` spent 1124s
        # and 117 steps on one lane with ``retry_attempts: {"claude-cli": 0}``
        # -- the probe was scheduled (``_race`` ends here) but never got a slot.
        # Giving up after this window is strictly better than a probe that can
        # never run: the lane it would have restored is a bonus, not the round.
        idle_deadline = time.monotonic() + HEALTHCHECK_IDLE_WAIT_SECONDS
        while self.pool.active_count and not self._stop_requested:
            if time.monotonic() >= idle_deadline:
                logger.info(
                    "Skipping engine reactivation probe for this round: the Sandbox "
                    "stayed busy for %.0fs (%s still unavailable).",
                    HEALTHCHECK_IDLE_WAIT_SECONDS,
                    ", ".join(sorted(self._unavailable_engine_ids)) or "none",
                )
                return
            await asyncio.sleep(0.05)
        if self._stop_requested or self._finalized:
            return
        if chat_retry:
            # An endpoint probe costs no Sandbox turn, so it runs even when the
            # CLI health path is unavailable.
            await self._probe_chat_profiles(chat_retry, retry=True)
        if not callable(health):
            return
        profiles = [
            profile
            for profile in self.engines
            if profile.engine_id in self._unavailable_engine_ids
            and str((profile.driver_profile or {}).get("protocol") or "")
            != "chat_completions"
        ]
        if not profiles:
            return
        workspace = str(self.graph.db_path.parent.parent)

        async def check(profile: EngineProfile) -> None:
            attempt = self._health_retry_attempts.get(profile.engine_id, 0) + 1
            self._health_retry_attempts[profile.engine_id] = attempt
            try:
                healthy, reason = await health(profile, workspace=workspace)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Same rationale as the first pass: a raised probe must not be
                # reported as the generic code, or the retry loop can never say
                # why it kept failing.
                detail = f"{type(error).__name__}: {error}"[:200]
                logger.warning(
                    "Engine health probe retry for %s raised: %s",
                    profile.engine_id,
                    detail,
                )
                healthy, reason = False, f"HEALTHCHECK_EXCEPTION:{type(error).__name__}"
            healthy = bool(healthy)
            reason_code = _safe_worker_reason(reason) or (
                "HEALTHY" if healthy else "WORKER_HEALTHCHECK_FAILED"
            )
            self.graph.emit_event(
                actor="coordinator",
                event_type=EventType.PREPARE_ENGINE_CHECKED,
                payload={
                    "engine_id": profile.engine_id,
                    "healthy": healthy,
                    "reason_code": reason_code,
                    "source": "worker_sandbox",
                    "retry": True,
                    "attempt": attempt,
                    "reactivated": healthy,
                },
            )
            if healthy:
                self._unavailable_engine_ids.discard(profile.engine_id)

        await asyncio.gather(*(check(profile) for profile in profiles))

    async def _prepare(self) -> None:
        self._change_phase(MutekiPhase.PREPARE)
        self.graph.emit_event(actor="coordinator", event_type=EventType.PHASE_CHANGED, payload={"phase": MutekiPhase.PREPARE.value})
        if self.official_worker is not None:
            await self.official_worker.start(
                run_id=self.graph.challenge_id,
                workspace=str(self.graph.db_path.parent.parent),
                account_root=self.config.official_account_root,
            )
            await self._check_official_worker_health()

    async def _check_official_worker_health(self) -> None:
        """Probe every Worker engine before the first Race Worker.

        The upstream Muteki Swarm treats a real one-turn health result as a
        dispatch prerequisite.  Keep that boundary at the production adapter:
        CLI profiles are tested inside the run Sandbox with the same projected
        credentials as a real Worker, while Chat-Completions profiles are probed
        over their own endpoint by :mod:`worker.chat_health` -- they never pass
        through the CLI driver, so the CLI probe cannot see them at all.
        """

        if self.official_worker is None:
            return
        health = getattr(self.official_worker, "health", None)
        chat_profiles = [
            profile
            for profile in self.engines
            if profile.healthy
            and str((profile.driver_profile or {}).get("protocol") or "")
            == "chat_completions"
        ]
        if not callable(health):
            # Chat profiles do not need the CLI driver, so they are still
            # probeable when the native Worker is unavailable.
            if chat_profiles:
                await self._probe_chat_profiles(chat_profiles)
            return
        profiles = [
            profile
            for profile in self.engines
            if profile.healthy
            and str((profile.driver_profile or {}).get("protocol") or "")
            != "chat_completions"
        ]
        if not profiles and not chat_profiles:
            return

        workspace = str(self.graph.db_path.parent.parent)

        async def check(profile: EngineProfile) -> None:
            detail = ""
            try:
                healthy, reason = await health(profile, workspace=workspace)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # A raised probe (missing credential file, unsupported argv,
                # container exec failure) used to collapse into one constant,
                # which is indistinguishable from a transient upstream blip.
                # The exception type is the only durable trace, so keep it.
                detail = f"{type(error).__name__}: {error}"[:200]
                logger.warning(
                    "Engine health probe for %s raised: %s", profile.engine_id, detail
                )
                healthy, reason = False, f"HEALTHCHECK_EXCEPTION:{type(error).__name__}"
            healthy = bool(healthy)
            reason_code = _safe_worker_reason(reason) or (
                "HEALTHY" if healthy else "WORKER_HEALTHCHECK_FAILED"
            )
            payload = {
                "engine_id": profile.engine_id,
                "healthy": healthy,
                "reason_code": reason_code,
                "source": "worker_sandbox",
            }
            if detail:
                payload["error_detail"] = detail
            self.graph.emit_event(
                actor="coordinator",
                event_type=EventType.PREPARE_ENGINE_CHECKED,
                payload=payload,
            )
            if healthy:
                # A later successful probe must clear the earlier verdict, or the
                # retry pass would keep reporting the engine as unavailable.
                self._unavailable_engine_ids.discard(profile.engine_id)
            else:
                self._unavailable_engine_ids.add(profile.engine_id)

        # The probe is one real model turn per engine, so a *transient* upstream
        # failure (an operator relay returning a momentary 4xx/5xx, a rate-limit
        # blip, a dropped stream) looks exactly like a dead engine.  Giving up
        # after a single pass abandons the whole Run before a single Intent
        # exists, which is a far worse outcome than spending another turn.  Keep
        # re-probing until the roster recovers or the retry window elapses.
        deadline = time.monotonic() + HEALTHCHECK_MAX_WAIT_SECONDS
        available: list[EngineProfile] = []
        while True:
            if profiles:
                await asyncio.gather(*(check(profile) for profile in profiles))
            if chat_profiles:
                await self._probe_chat_profiles(chat_profiles)
            available = [
                profile
                for profile in self.engines
                if profile.healthy
                and profile.engine_id not in self._unavailable_engine_ids
            ]
            if available or self._stop_requested or time.monotonic() >= deadline:
                break
            await asyncio.sleep(HEALTHCHECK_RETRY_DELAY_SECONDS)

        if (profiles or chat_profiles) and not available:
            self._stop_reason = "NO_HEALTHY_WORKER_PROFILE"
            self._stop_requested = True
            self.graph.add_dead_end(
                actor="coordinator",
                description="NO_HEALTHY_WORKER_PROFILE",
            )
    async def _probe_chat_profiles(
        self, profiles: list[EngineProfile], *, retry: bool = False
    ) -> None:
        """Probe Chat-Completions engines over their own endpoint.

        These engines never enter the CLI probe, so without this pass a relay
        that answers 403/503 (or answers without a tool call) stays "healthy"
        by default and keeps receiving Workers, which then report
        ``tool_steps: 0`` and consume the whole round.
        """

        async def check(profile: EngineProfile) -> None:
            try:
                healthy, reason = await probe_chat_worker(profile)
            except Exception:
                healthy, reason = False, "WORKER_HEALTHCHECK_FAILED"
            healthy = bool(healthy)
            reason_code = _safe_worker_reason(reason) or (
                "HEALTHY" if healthy else "WORKER_HEALTHCHECK_FAILED"
            )
            payload: dict[str, Any] = {
                "engine_id": profile.engine_id,
                "healthy": healthy,
                "reason_code": reason_code,
                "source": "worker_chat_endpoint",
            }
            if retry:
                payload["retry"] = True
                payload["reactivated"] = healthy
            self.graph.emit_event(
                actor="coordinator",
                event_type=EventType.PREPARE_ENGINE_CHECKED,
                payload=payload,
            )
            if healthy:
                self._unavailable_engine_ids.discard(profile.engine_id)
            else:
                self._unavailable_engine_ids.add(profile.engine_id)

        await asyncio.gather(*(check(profile) for profile in profiles))

    def _record_first_action_injection(self, first_action: str, goal: str, *, lane: str) -> None:
        """Record that a FIRST ACTION was placed in a lane goal, and where.

        Race proposes no Intent, so the goal it was dispatched with is not
        persisted by anything else.  Without this event, ``run_events`` cannot
        distinguish "the Worker ignored the FIRST ACTION" from "the FIRST ACTION
        was never in the prompt" -- and the 2026-11-07 audit found both cases in
        the population: 87 of 347 Runs never dispatched a Race Worker at all.

        ``in_window`` is the one field that needs arithmetic rather than
        observation.  ``cli_solver`` renders ``intent_goal[:600]``, and
        ``official_worker`` appends the action contract after the goal, so the
        cut is applied to the concatenation -- a goal whose FIRST ACTION ends
        before ``600 - len(contract_suffix)`` survives intact.  Recording it per
        Run turns a silent truncation into a queryable fact.
        """

        # Mirrors ``official_worker``'s contract suffix and ``cli_solver``'s cut.
        contract_suffix = "\nStay within this action contract and do not switch tool domains: "
        visible = max(0, 600 - len(contract_suffix))
        fa_end = goal.find(first_action) + len(first_action) if first_action in goal else 0
        self.graph.emit_event(
            actor="coordinator",
            event_type=EventType.EXPLOIT_FIRST_ACTION,
            payload={
                "lane": lane,
                "text": first_action,
                "chars": len(first_action),
                "source": str(getattr(self.config, "exploit_first_action_source", "") or ""),
                "verdict": str(getattr(self.config, "exploit_first_action_verdict", "") or ""),
                "goal_chars": len(goal),
                "goal_head": goal[:180],
                "in_window": bool(fa_end and fa_end <= visible),
            },
        )

    def _record_probe_injection(self, probe: str, goal: str, *, lane: str) -> None:
        """Record that a PROBE (unmeasured recipe) was placed in a lane goal.

        Same reason as ``_record_first_action_injection``, for the other channel.
        Without it, ``run_events`` shows a box that received a concrete first
        command as "no first action available" -- and since 24 of the 26 reachable
        UNSOLVED boxes arrive through this channel, that would make an adoption
        audit systematically blind to almost the whole improvement.

        Deliberately a separate event type, so "did the Worker follow the measured
        order" and "did the probe even apply" stay separable in a query.
        """

        contract_suffix = "\nStay within this action contract and do not switch tool domains: "
        visible = max(0, 600 - len(contract_suffix))
        probe_end = goal.find(probe) + len(probe) if probe in goal else 0
        self.graph.emit_event(
            actor="coordinator",
            event_type=EventType.EXPLOIT_PROBE_ACTION,
            payload={
                "lane": lane,
                "text": probe,
                "chars": len(probe),
                "goal_chars": len(goal),
                "goal_head": goal[:180],
                "in_window": bool(probe_end and probe_end <= visible),
            },
        )

    async def _race(self) -> None:
        self._change_phase(MutekiPhase.RACE)
        profiles = [
            profile
            for profile in self.engines
            if profile.healthy
            and profile.engine_id not in self._unavailable_engine_ids
        ]
        # The native Muteki Worker owns its own Sandbox/solver lifecycle.  The
        # legacy callback path does not: it commonly closes over one
        # ToolGateway/SQLAlchemy session.  Running several Race callbacks at
        # once against that shared session causes ``Session is already
        # flushing`` and loses the Race handoff before the Coordinator can
        # read the Blackboard.  Keep the compatibility path to one bounded
        # scout; native external workers retain the official multi-engine Race
        # semantics because their adapter boundary is explicit.
        if self.pool.external_runner is None and self.pool.engine_pool is None:
            profiles = profiles[:1]
        # Backfill: a failed health check must not shrink the Run to "one lane"
        # without anyone noticing.  The previous behaviour was to dispatch the
        # surviving engine with the exploit goal and leave the missing engine's
        # lane unowned -- the two accepted Runs in the 2026-10-21 arena batch
        # both had *both* engines healthy, while all three unsolved ones had
        # exactly one failed probe.  The surviving engine is told it owns the
        # missing lane too, and the handoff is recorded as an event.
        missing_engine_ids = [
            profile.engine_id
            for profile in self.engines
            if profile.engine_id in self._unavailable_engine_ids
        ]
        backfill_prefix = ""
        if missing_engine_ids and profiles:
            backfill_prefix = _BACKFILL_GOAL_PREFIX.format(missing=len(missing_engine_ids))
            self.graph.emit_event(
                actor="coordinator",
                event_type=EventType.ENGINE_BACKFILL,
                payload={
                    "missing_engines": missing_engine_ids,
                    "healthy_engines": [profile.engine_id for profile in profiles],
                    "retry_attempts": {
                        engine_id: int(self._health_retry_attempts.get(engine_id, 0))
                        for engine_id in missing_engine_ids
                    },
                    "assigned_lane": _RACE_LANES[0]["lane_key"],
                    "reason": "ENGINE_UNAVAILABLE",
                },
            )
        # Authentication and quota have already been exercised by the
        # production Sandbox health gate in ``_prepare``.  A failed CLI profile
        # must never reach this dispatch point, otherwise the whole Race window
        # is spent waiting for an engine that cannot complete a turn.
        if len(profiles) <= 1:
            # A single healthy engine used to be dispatched with NO goal at all,
            # which is the same whole-challenge reconnaissance that never gets
            # past fingerprinting before the bounded window expires.  It gets
            # the exploit lane instead: read ./AGENTS.md, send the recipe, and
            # only explore if there is no recipe for this box.
            for profile in profiles:
                single_goal = _SINGLE_WORKER_GOAL
                first_action = str(getattr(self.config, "exploit_first_action", "") or "")
                # No recipe matched: hand over the candidate technique classes
                # instead of the bare template.  The recipe path answers "how do
                # I exploit X" and is keyed on identifiers, so an ordinary Web box
                # (``[极客大挑战 2019]EasySQL``) has no CVE, no advisory number and
                # therefore no recipe at all -- it used to reach this lane with
                # nothing but "explore".  The technique clause names what to test
                # first; it is a decision procedure, not a payload.
                technique_goal = str(getattr(self.config, "technique_candidate_goal", "") or "")
                if first_action:
                    # The FIRST ACTION now comes BEFORE the "./AGENTS.md" pointer,
                    # and it did not use to.
                    #
                    # Measured 2026-11-07 over the 178 Runs of Challenges that had a
                    # FIRST ACTION available: **68%** opened with an orientation
                    # step instead of a target-facing probe -- 27% read the
                    # blackboard skill, 21% read ./AGENTS.md, 18% listed the
                    # workspace -- and only 30% opened by touching the target.
                    # Zero of the 178 issued the FIRST ACTION as command #0.
                    #
                    # The old wording was "The curated recipe for this box is in
                    # ./AGENTS.md and its step order IS your plan ... FIRST ACTION:
                    # <x>".  That puts a *file to read* two clauses ahead of the
                    # *command to run*, and an agent that follows instructions in
                    # order does the read first.  The read is not the problem --
                    # the problem is that it costs the first command of a bounded
                    # round, which is exactly the command the recipe exists to
                    # spend.  So the order is inverted and the read is deferred to
                    # after the action, where it is still available for step 2.
                    #
                    # Length: this head is 4 chars longer than the old one, and
                    # the tail is unchanged in meaning.  ``intent_goal`` is
                    # truncated at 600 chars *from the tail* and the backfill
                    # prefix is prepended, so putting the highest-value clause
                    # first is also what keeps it inside the window when the
                    # prefix fires -- the tail sentence is the part that may be
                    # lost, and it is the part that only says what to do after a
                    # failure.
                    single_goal = (
                        "Only you are running. FIRST ACTION -- run it as your very first "
                        "command, before any recon, board read or AGENTS.md read: "
                        f"{first_action} "
                        "Then read ./AGENTS.md for the rest of the step order. If the "
                        "action fails, take the next recipe step, not a fresh scan."
                    )
                    gate = str(getattr(self.config, "exploit_stop_loss_gate", "") or "")
                    if gate:
                        # Prefixed, not appended: the gate has to be read before
                        # the first action is chosen, and ``intent_goal`` is
                        # truncated from the tail at 600 chars.
                        single_goal = f"{gate} {single_goal}"
                elif technique_goal:
                    # No measured recipe, but an UNMEASURED one matched this box
                    # and it names an executable first line.  A specific
                    # unverified request beats a generic technique class: it is
                    # the difference between "try SSTI somewhere" and "POST
                    # /service/extdirect".  It is offered as a probe (verify, then
                    # continue) rather than an order, so a wrong recipe costs one
                    # command instead of the round.
                    probe = str(getattr(self.config, "exploit_probe_action", "") or "")
                    single_goal = _probe_goal(probe, technique_goal) if probe else technique_goal
                    if probe:
                        self._record_probe_injection(
                            probe, backfill_prefix + single_goal, lane="exploit:known-path"
                        )
                if first_action:
                    self._record_first_action_injection(
                        first_action, backfill_prefix + single_goal, lane="exploit:known-path"
                    )
                await self._spawn(
                    profile,
                    role="race",
                    goal=backfill_prefix + single_goal,
                    payload={
                        "route_hash": "race:exploit:known-path",
                        "branch_id": "branch:exploit:known-path",
                        "lane_key": "exploit:known-path",
                        "race_lane": "exploit:known-path",
                    },
                )
        else:
            for index, profile in enumerate(profiles):
                lane = _RACE_LANES[index % len(_RACE_LANES)]
                # A lane may demand a specific transport.  ``intel:public-poc``
                # needs ``chat_completions`` because ``lookup_public_poc`` is
                # only bound on that protocol; handing it to a CLI Worker would
                # reproduce exactly the failure this lane exists to fix -- a
                # goal that names a tool the Worker cannot call.
                required = lane.get("requires_protocol")
                if required and str(
                    (profile.driver_profile or {}).get("protocol") or ""
                ) != required:
                    lane = _RACE_LANES[0]
                cycle = index // len(_RACE_LANES) + 1
                lane_key = lane["lane_key"] if cycle == 1 else f"{lane['lane_key']}:{cycle}"
                route_hash = f"race:{lane_key}"
                template = lane.get("goal_template") or (
                    "Race reconnaissance lane: {label}. Do not duplicate other lanes."
                )
                goal = template.format(label=lane["label"])
                first_action = str(getattr(self.config, "exploit_first_action", "") or "")
                if lane_key.startswith("exploit:") and first_action:
                    # The recipe's first move goes in the goal itself.  The
                    # "read ./AGENTS.md" instruction above is one the Worker can
                    # satisfy and then ignore -- measured: Solr read the file and
                    # still spent 71 requests on the wrong endpoint.
                    #
                    # The action now leads, for the same measured reason as the
                    # single-worker path above: 68% of the Runs that had a FIRST
                    # ACTION available opened by reading something instead of
                    # touching the target, and none issued it as command #0.
                    goal = (
                        "Exploitation lane. FIRST ACTION -- run it as your very first "
                        f"command, before any recon or AGENTS.md read: {first_action} "
                        "Then read ./AGENTS.md for the rest of the step order. If that "
                        "action fails, continue with the next recipe step, not a fresh scan."
                    )
                    gate = str(getattr(self.config, "exploit_stop_loss_gate", "") or "")
                    if gate:
                        # Same truncation reason as the single-worker path.
                        goal = f"{gate} {goal}"
                    self._record_first_action_injection(
                        first_action, goal, lane=lane_key
                    )
                elif lane_key.startswith("exploit:"):
                    # Same reason as the single-engine path: with no recipe the
                    # exploit lane would otherwise be the generic "look up the
                    # recipe, else explore" text, which on a box that HAS no
                    # recipe degrades to "explore".
                    technique_goal = str(
                        getattr(self.config, "technique_candidate_goal", "") or ""
                    )
                    if technique_goal:
                        goal = technique_goal
                    probe = str(getattr(self.config, "exploit_probe_action", "") or "")
                    if probe:
                        goal = _probe_goal(probe, goal)
                        self._record_probe_injection(probe, goal, lane=lane_key)
                await self._spawn(
                    profile,
                    role="race",
                    goal=goal,
                    payload={
                        "route_hash": route_hash,
                        "branch_id": f"branch:{lane_key}",
                        "lane_key": lane_key,
                        "race_lane": lane_key,
                    },
                )
        if not self.pool.active_count:
            return
        try:
            round_finished = await self._await_worker_round(
                timeout=max(
                    1.0,
                    float(self.config.worker_timeout_seconds)
                    + max(0.0, float(self.config.worker_wait_grace_seconds)),
                ),
                # Race is a bounded scout for one verified flag.  Ending the
                # wait as soon as that flag lands stops a solved Run from
                # burning the rest of its Worker budget on lanes that can no
                # longer change the outcome.
                stop_on_flag=True,
            )
        except asyncio.TimeoutError:
            # Official Swarm treats Race as a bounded scout.  A timed-out scout
            # must not discard facts already written to the shared Blackboard;
            # recover the Worker and continue through the Coordinator slow path.
            self.graph.add_dead_end(actor="coordinator", description=TIMED_OUT_STOP_REASON)
            # Record the *cause* separately from the dead end.  The dead end is
            # a graph edge; this flag is what lets the terminal reason say
            # TIMEOUT instead of STOPPED, so the Run does not close as
            # "finished, unsolved" when it was really "ran out of time".
            self._timed_out = True
            await self._recover_timed_out_workers()
        else:
            if not round_finished:
                # A stop request during Race must not wait out the scout
                # window: return so ``run`` can hand the active Worker to
                # Finalize and publish the terminal event.
                return
        self._release_completed_semantics()
        self._schedule_health_retry()

    async def _recover_timed_out_workers(self) -> None:
        """Reclaim a timed-out Worker without terminating the solve.

        This mirrors the upstream Swarm boundary: one Worker turn may be
        abandoned, while the run-scoped Sandbox, Graph and Evidence remain
        available to the next Reason/Worker cycle.
        """

        active_jobs = tuple(self.pool.active_jobs())
        overdue_keys_reader = getattr(self.official_worker, "overdue_job_keys", None)
        overdue_keys = (
            set(overdue_keys_reader())
            if callable(overdue_keys_reader)
            else {
                "|".join((str(job.worker_id or ""), str(job.intent_id or "")))
                for job in active_jobs
            }
        )
        timed_out_worker_ids: set[str] = set()
        if self.official_worker is not None:
            cancel_active = getattr(self.official_worker, "cancel_active", None)
            if callable(cancel_active):
                for job in active_jobs:
                    job_key = "|".join(
                        (
                            str(job.worker_id or ""),
                            str(job.intent_id or ""),
                        )
                    )
                    if job_key not in overdue_keys:
                        continue
                    await cancel_active(job_key=job_key)
                    timed_out_worker_ids.add(str(job.worker_id))
        if self._health_retry_task is not None and not self._health_retry_task.done():
            self._health_retry_task.cancel()
            await asyncio.gather(self._health_retry_task, return_exceptions=True)
        await self.pool.cancel_workers(timed_out_worker_ids)
        self._release_completed_semantics()

    async def _dispatch_open_intent(self) -> None:
        capacity = min(self.pool.max_workers, self.stage_policy.get_max_workers(self.phase))
        while self.pool.active_count < capacity:
            selector = getattr(self.graph, "dispatchable_intents", None)
            intents = list(selector()) if callable(selector) else list(self.graph.intents(status="open"))
            if not intents:
                return
            profile = self._next_available_profile()
            if profile is None:
                return
            item = self._select_route_intent(intents)
            if item is None:
                return
            if not await self._spawn(profile, role="explore", intent_id=item.intent_id, goal=item.description, payload=item.payload or {}):
                return
            self._dispatched_intents.add(item.intent_id)

    async def _dispatch_review(self, *, goal: str = "review shared blackboard") -> None:
        if not self.stage_policy.can_spawn(self.phase, "review"):
            return
        if self.pool.active_count >= min(self.pool.max_workers, self.stage_policy.get_max_workers(self.phase)):
            return
        profile = self._next_available_profile()
        if profile is not None:
            await self._spawn(profile, role="review", goal=goal)

    async def _dispatch_bootstrap(self) -> None:
        """Run one bounded official whole-challenge recovery worker.

        This mirrors Muteki's re-bootstrap path after a barren Reason cycle:
        the worker receives the existing Blackboard as a head-start and the
        official CLI/Sandbox owns the next exploratory turn.  It is available
        only for the explicit upstream worker backends; the normal callback
        path remains Intent-bound and unchanged.
        """

        if self.config.worker_backend not in {"upstream_local", "upstream_container"}:
            return
        limit = max(0, int(self.config.max_bootstrap_workers))
        if limit > 0 and self._bootstrap_workers >= limit:
            return
        capacity = min(self.pool.max_workers, self.stage_policy.get_max_workers(self.phase))
        if self.pool.active_count >= capacity:
            return
        profile = self._next_available_profile()
        if profile is None:
            return
        goal = (
            "The current evidence-backed route produced no verified finding. "
            "Treat the existing SharedGraph as a head-start, avoid concluded "
            "directions, and pursue a materially different bounded route toward "
            "the challenge goal. Continue only with real evidence and stop if "
            "the official provenance gate has no supported result."
        )
        if await self._spawn(profile, role="bootstrap", goal=goal):
            self._bootstrap_workers += 1
            self.graph.emit_event(
                actor="coordinator",
                event_type=EventType.COORDINATOR_DIRECTIVE,
                payload={
                    "directive": "rebootstrap",
                    "reason_code": "REASON_ROUTE_EXHAUSTED",
                    "worker_role": "bootstrap",
                    "attempt": self._bootstrap_workers,
                },
            )

    async def _spawn(self, profile: EngineProfile, *, role: str, intent_id: str | None = None, goal: str = "", payload: dict | None = None) -> bool:
        if not self.stage_policy.can_spawn(self.phase, role):
            return False
        if self.pool.active_count >= min(self.pool.max_workers, self.stage_policy.get_max_workers(self.phase)):
            return False
        if not self._a_round_still_fits():
            # Declining is deliberately paired with convergence: the loop must
            # drain the rounds that are already running and leave through the
            # normal path, not idle until the no-progress guard trips.
            self._reserve_the_last_round()
            return False
        worker_budget = self.config.max_total_workers
        if worker_budget is not None and self._spawned_workers >= max(1, int(worker_budget)):
            if not self._budget_exhausted:
                self._budget_exhausted = True
                self._stop_reason = "WORKER_BUDGET_EXHAUSTED"
                self.graph.add_dead_end(
                    actor="coordinator",
                    description="WORKER_BUDGET_EXHAUSTED",
                )
            return False
        self._worker_number += 1
        worker_id = f"worker-{self._worker_number}"
        resolved_payload = dict(payload or {})
        reservation = None
        if intent_id:
            item = next((candidate for candidate in self.graph.intents() if candidate.intent_id == intent_id), None)
            if item is not None:
                resolved_payload = {**dict(item.payload or {}), **resolved_payload}
            resolved_payload["route_hash"] = stable_route_hash(resolved_payload, goal=goal)
            resolved_payload["branch_id"] = stable_branch_id(
                resolved_payload["route_hash"],
                str(resolved_payload.get("branch_id") or ""),
            )
            decision = acquire_dispatch_semantics(
                self.graph,
                worker_id=worker_id,
                intent_id=intent_id,
                payload=resolved_payload,
            )
            if not decision.allowed:
                self.graph.release_intent(worker=worker_id, intent_id=intent_id)
                self._worker_number -= 1
                return False
            reservation = decision.reservation
            claim_lease_seconds = max(
                300.0,
                float(self.config.worker_timeout_seconds)
                + max(0.0, float(self.config.worker_wait_grace_seconds))
                + 300.0,
            )
            if not self.graph.claim_intent(
                worker=worker_id,
                intent_id=intent_id,
                lease_s=claim_lease_seconds,
            ):
                if reservation is not None:
                    release_dispatch_semantics(self.graph, reservation)
                self._worker_number -= 1
                return False
        elif resolved_payload:
            resolved_payload["route_hash"] = stable_route_hash(resolved_payload, goal=goal)
            resolved_payload["branch_id"] = stable_branch_id(
                resolved_payload["route_hash"],
                str(resolved_payload.get("branch_id") or ""),
            )
            decision = acquire_dispatch_semantics(
                self.graph,
                worker_id=worker_id,
                intent_id=intent_id or f"{role}:{worker_id}",
                payload=resolved_payload,
            )
            if not decision.allowed:
                self._worker_number -= 1
                return False
            reservation = decision.reservation
        route_hash = str(resolved_payload.get("route_hash") or stable_route_hash(resolved_payload, goal=goal))
        branch_id = str(resolved_payload.get("branch_id") or stable_branch_id(route_hash))
        if route_hash in self._active_route_hashes:
            self._worker_number -= 1
            if intent_id:
                self.graph.release_intent(worker=worker_id, intent_id=intent_id)
            if reservation is not None:
                release_dispatch_semantics(self.graph, reservation)
            return False
        engine_attempt_id = f"{self.graph.challenge_id}:{profile.engine_id}:{worker_id}:{route_hash}"
        resolved_payload["route_hash"] = route_hash
        resolved_payload["branch_id"] = branch_id
        resolved_payload["engine_attempt_id"] = engine_attempt_id
        record_attempt = getattr(self.graph, "record_engine_attempt", None)
        if intent_id and callable(record_attempt):
            record_attempt(
                intent_id=intent_id,
                engine_attempt_id=engine_attempt_id,
                actor="coordinator",
            )
        container_paths = self.config.worker_backend == "upstream_container"
        worker_workspace = (
            _CONTAINER_WORKSPACE
            if container_paths
            else str(self.graph.db_path.parent.parent)
        )
        worker_blackboard_db = (
            _container_graph_path(self.graph.db_path)
            if container_paths
            else str(self.graph.db_path)
        )
        job = WorkerJob(
            worker_id=worker_id,
            role=role,
            engine_id=profile.engine_id,
            graph_path=str(self.graph.db_path),
            challenge_id=self.graph.challenge_id,
            intent_id=intent_id,
            goal=goal,
            payload=resolved_payload,
            driver_profile=dict(profile.driver_profile or {}),
            route_hash=route_hash,
            branch_id=branch_id,
            engine_attempt_id=engine_attempt_id,
            environment={
                **dict(profile.environment),
                "MUTEKI_WORKER_ID": worker_id,
                "MUTEKI_INTENT_ID": str(intent_id or ""),
                "MUTEKI_CHALLENGE_ID": self.graph.challenge_id,
                "MUTEKI_FLAG_PATTERN": self._flag_pattern(),
                "MUTEKI_BLACKBOARD_DB": worker_blackboard_db,
                "MUTEKI_WORKSPACE": worker_workspace,
                "MUTEKI_BLACKBOARD_SCRIPT": (
                    "/usr/local/bin/blackboard.py"
                    if container_paths
                    else os.environ.get("MUTEKI_BLACKBOARD_SCRIPT", "blackboard.py")
                ),
            },
        )
        spawned = await self.pool.spawn(job)
        if spawned:
            self._spawned_workers += 1
            self._active_route_hashes.add(route_hash)
        if not spawned:
            if intent_id:
                self.graph.release_intent(worker=worker_id, intent_id=intent_id)
            if reservation is not None:
                release_dispatch_semantics(self.graph, reservation)
        elif spawned and reservation is not None:
            self._semantic_reservations[worker_id] = reservation
        return spawned

    def _next_available_profile(self) -> EngineProfile | None:
        """Choose the next healthy engine using a fair rotating cursor."""

        candidates = [
            profile
            for profile in self.engines
            if profile.healthy
            and profile.engine_id not in self._unavailable_engine_ids
            and profile.engine_id not in self.pool.active_engine_ids
        ]
        if not candidates:
            return None
        # Preserve configured engine order while rotating the starting point.
        for offset in range(len(self.engines)):
            index = (self._engine_cursor + offset) % len(self.engines)
            profile = self.engines[index]
            if profile in candidates:
                self._engine_cursor = (index + 1) % len(self.engines)
                return profile
        return candidates[0]

    def _select_route_intent(self, intents):
        """Select one new route, or an explicitly review-approved retry.

        Verification units are dispatched first.  A candidate the swarm already
        produced is cheaper to settle than a fresh direction is to explore, and
        it is the only way a pending fact ever leaves the "待验证" set -- so when
        the Review pass has queued verification work, the next Worker turn
        spends itself there instead of opening yet another route.
        """

        all_items = list(self.graph.intents())
        completed_routes = {
            self._intent_route(item)
            for item in all_items
            if str(item.status).casefold() == "done" and self._intent_route(item)
        }
        active_routes = {
            reservation.route_hash
            for reservation in self._semantic_reservations.values()
            if reservation.route_hash
        }
        active_routes.update(self._active_route_hashes)
        seen: set[str] = set()

        def _select(*, verification: bool):
            for item in intents:
                if is_verification_unit(item) is not verification:
                    continue
                route = self._intent_route(item)
                if route and route in active_routes:
                    continue
                if route and route in seen:
                    continue
                if (
                    route
                    and route in completed_routes
                    and not self._review_approved_retry(item.payload)
                ):
                    continue
                if route:
                    seen.add(route)
                return item
            return None

        return _select(verification=True) or _select(verification=False)

    @staticmethod
    def _intent_route(item) -> str:
        return stable_route_hash(
            dict(item.payload or {}),
            intent_id=str(item.intent_id or ""),
            goal=str(item.description or ""),
        )

    @staticmethod
    def _review_approved_retry(payload: Mapping[str, object] | None) -> bool:
        values = dict(payload or {})
        if any(bool(values.get(key)) for key in ("review_approved", "review_approved_retry")):
            return True
        if values.get("retry_of"):
            return True
        rationale = str(values.get("rationale") or "").casefold()
        return "route reopened by review" in rationale

    def _release_completed_semantics(self) -> None:
        """Release one-shot worker locks after the pool's wait barrier."""

        if self.pool.active_count:
            return
        for reservation in tuple(self._semantic_reservations.values()):
            release_dispatch_semantics(self.graph, reservation)
        self._semantic_reservations.clear()
        self._active_route_hashes.clear()

    async def _review_runner(self, job: WorkerJob) -> WorkerOutcome:
        result = ReviewWorker(self.graph, worker_id=job.worker_id).run()
        apply_review_proposals = getattr(self.graph, "apply_review_proposals", None)
        decisions = apply_review_proposals(actor="coordinator") if callable(apply_review_proposals) else []
        accepted = sum(1 for item in decisions if item.get("decision") == "accepted")
        deferred = sum(1 for item in decisions if item.get("decision") == "deferred")
        return WorkerOutcome(
            job.worker_id,
            "COMPLETED",
            result=(
                f"facts={len(result.suspicious_fact_ids)};"
                f"branches={len(result.branch_intent_ids)};"
                f"review_accepted={accepted};review_deferred={deferred}"
            ),
        )

    async def _external_worker_runner(self, job: WorkerJob) -> WorkerOutcome:
        workspace = str(job.environment.get("MUTEKI_WORKSPACE") or self.graph.db_path.parent.parent)
        if not job.intent_id:
            return WorkerOutcome(job.worker_id, "FAILED", result="EXTERNAL_WORKER_REQUIRES_INTENT")
        if self.config.worker_backend == "cli" and self.cli_driver is not None:
            result = await self.cli_driver.run_worker(
                intent_id=job.intent_id,
                engine=job.engine_id,
                workspace=workspace,
                blackboard=str(self.graph.db_path),
                worker_id=job.worker_id,
                timeout_seconds=self.config.worker_timeout_seconds,
            )
        elif self.config.worker_backend == "container" and self.container_executor is not None:
            result = await self.container_executor.run_worker_async(
                job.intent_id,
                workspace,
                str(self.graph.db_path),
                engine=job.engine_id,
                timeout=self.config.worker_timeout_seconds,
                worker_id=job.worker_id,
                challenge_id=self.graph.challenge_id,
            )
        else:
            return WorkerOutcome(job.worker_id, "FAILED", result="EXTERNAL_WORKER_NOT_CONFIGURED")
        status = "COMPLETED" if result.returncode == 0 else "FAILED"
        detail = result.stderr or result.stdout
        return WorkerOutcome(
            job.worker_id,
            status,
            result=detail[-2000:],
            result_code=(
                WorkerResultCode.COMPLETED.value
                if status == "COMPLETED"
                else WorkerResultCode.WORKER_FAILURE.value
            ),
        )

    async def _official_worker_runner(self, job: WorkerJob) -> WorkerOutcome:
        # Bound before the Worker starts so a step callback arriving from the
        # Worker's own thread knows which loop to publish on.
        self._bind_runtime_loop()
        if self.official_worker is None:
            return WorkerOutcome(
                job.worker_id,
                "FAILED",
                result="OFFICIAL_WORKER_NOT_CONFIGURED",
                result_code=WorkerResultCode.EXTERNAL_BLOCKER.value,
                failure_reason="OFFICIAL_WORKER_NOT_CONFIGURED",
            )
        native_cursor = self._native_event_cursor()
        result: OfficialWorkerResult = await self.official_worker.execute(job)
        # The Worker's stream is over, so publish its trailing step aggregate
        # now — before any of the return paths below — or the last (usually most
        # diagnostic) slice of the round would be coalesced away.
        self._flush_worker_steps(
            job.worker_id,
            result=str(getattr(result, "status", "") or ""),
            result_code=str(
                (result.metadata or {}).get("result_code")
                if isinstance(getattr(result, "metadata", None), Mapping)
                else ""
            ),
        )
        usage_bridge = getattr(self, "official_worker_usage_bridge", None)
        if usage_bridge is not None:
            try:
                usage_result = usage_bridge(job, result)
                if inspect.isawaitable(usage_result):
                    await usage_result
            except Exception:
                # Accounting must remain observability-only.  A failed metrics
                # write cannot turn a bounded Worker result into a solver
                # execution failure.
                self.graph.emit_event(
                    actor=job.worker_id,
                    event_type="worker.usage.record_failed",
                    payload={"reason_code": "OFFICIAL_WORKER_USAGE_RECORD_FAILED"},
                )
        dead_end = self._native_dead_end_signal(job, native_cursor)
        if dead_end is None:
            candidate_signal = getattr(result, "dead_end_signal", None)
            if candidate_signal is not None:
                dead_end = candidate_signal
        if dead_end is None and str((result.metadata or {}).get("result_code") or "").upper() == WorkerResultCode.ROUTE_EXHAUSTED.value:
            from .outcomes import DeadEndKind, DeadEndSignal

            dead_end = DeadEndSignal(
                kind=DeadEndKind.ROUTE_EXHAUSTED,
                reason="No new endpoint remains on the assigned route.",
                route_hash=stable_route_hash(
                    job.payload,
                    intent_id=str(job.intent_id or ""),
                    goal=job.goal,
                ),
                evidence_refs=tuple(getattr(result, "evidence_refs", ()) or ()),
            )
        if dead_end is None:
            dead_end = signal_from_worker_text(
                getattr(result, "output", ""),
                payload=getattr(job, "payload", {}) or {},
                intent_id=str(job.intent_id or ""),
                goal=str(getattr(job, "goal", "") or ""),
                evidence_refs=tuple(getattr(result, "evidence_refs", ()) or ()),
            )
        if dead_end is not None:
            # Native CliSolver has already appended this event to the official
            # SharedGraph.  The pool consumes the signal to keep intent/result
            # semantics uniform, while avoiding a second Graph dead-end.
            if not dead_end.already_persisted:
                self._conclude_official_intent(job, dead_end.kind.value)
            return WorkerOutcome(
                job.worker_id,
                "COMPLETED",
                result=dead_end.reason,
                result_code=dead_end.kind.value.upper(),
                dead_end=dead_end,
            )
        if result.success and not result.evidence_refs:
            # Native execution is never authoritative by itself.  Without an
            # injected bridge to the existing Evidence authority, fail closed
            # instead of allowing a successful CLI result to advance the graph.
            emit_event = getattr(self.graph, "emit_event", None)
            if callable(emit_event):
                emit_event(
                    actor=job.worker_id,
                    event_type="worker.blocked",
                    payload={"reason_code": "EVIDENCE_BRIDGE_REQUIRED"},
                )
            return WorkerOutcome(
                job.worker_id,
                "FAILED",
                result="EVIDENCE_BRIDGE_REQUIRED",
                result_code=WorkerResultCode.EXTERNAL_BLOCKER.value,
                failure_reason="EVIDENCE_BRIDGE_REQUIRED",
            )
        if result.status not in {"COMPLETED"}:
            result_code = failure_code(result.status, result.metadata)
            failure_reason = _safe_worker_reason(
                (result.metadata or {}).get("reason")
                if isinstance(result.metadata, Mapping)
                else ""
            ) or _safe_worker_reason(getattr(result, "status", ""))
            self._conclude_official_intent(job, result_code.value)
            return WorkerOutcome(
                job.worker_id,
                "FAILED",
                result=result.status,
                result_code=result_code.value,
                failure_reason=failure_reason,
            )
        verified_flag = ""
        if result.success and result.evidence_refs:
            self._record_official_evidence(job, result, native_cursor=native_cursor)
            verified_flag = ""
            metadata = result.metadata if isinstance(result.metadata, dict) else {}
            candidate = str(metadata.get("verified_flag") or "").strip()
            if candidate:
                flag_found = getattr(self.graph, "flag_found", None)
                if callable(flag_found):
                    flag_found(
                        actor=job.worker_id,
                        flag=candidate,
                        artifact_id=str(result.evidence_refs[0]),
                        intent_id=job.intent_id,
                    )
                    verified_flag = candidate
            self._conclude_official_intent(
                job,
                "FLAG_VERIFIED" if verified_flag else "EVIDENCE_RECORDED",
            )
        elif not result.success:
            result_code = failure_code(result.status, result.metadata)
            self._conclude_official_intent(job, result_code.value)
            return WorkerOutcome(
                job.worker_id,
                "FAILED",
                result=result.status,
                result_code=result_code.value,
                failure_reason=_safe_worker_reason(
                    (result.metadata or {}).get("reason")
                    if isinstance(result.metadata, Mapping)
                    else ""
                ),
            )
        # The native worker is deliberately not promoted to a solved finding here.
        # It must write evidence-backed facts through the graph/Evidence adapter.
        return WorkerOutcome(
            job.worker_id,
            "COMPLETED" if result.success else "FAILED",
            flag_found=bool(verified_flag),
            result=result.status,
            result_code=WorkerResultCode.COMPLETED.value if result.success else WorkerResultCode.WORKER_FAILURE.value,
        )

    def _native_dead_end_signal(self, job: WorkerJob, cursor: int | None):
        """Read explicit dead-end markers emitted by the native SharedGraph."""

        if cursor is None:
            return None
        reader = getattr(self.graph, "native_dead_end_events_since", None)
        if not callable(reader):
            return None
        try:
            events = reader(cursor)
        except (OSError, RuntimeError, TypeError, ValueError):
            return None
        for event in events:
            event_actor = str(event.get("actor") or "") if isinstance(event, Mapping) else ""
            if event_actor and event_actor not in {
                str(job.worker_id),
                str(job.engine_id),
            }:
                # Race Workers share the native graph.  Do not attribute a
                # different Worker's explicit route ruling to this job.
                continue
            payload = event.get("payload") if isinstance(event, Mapping) else {}
            reason = payload.get("reason") if isinstance(payload, Mapping) else ""
            signal = signal_from_worker_text(
                f"DEADEND={reason}",
                payload=job.payload,
                intent_id=str(job.intent_id or ""),
                goal=job.goal,
                already_persisted=True,
            )
            if signal is not None:
                return signal
        return None

    def _reason_provider_telemetry(self) -> dict[str, object]:
        """Expose Reason selection/fallback state without model content."""

        provider = getattr(self.reason, "provider", None)
        if provider is None:
            return {
                "provider": "none",
                "provider_kind": "deterministic_fallback",
            }
        telemetry = {
            "provider": type(provider).__name__[:100],
            "provider_kind": (
                "coordinator_model"
                if hasattr(provider, "last_error_code")
                else "deterministic_strategy"
            ),
            "provider_error_code": str(getattr(provider, "last_error_code", "") or "")[:80],
        }
        if hasattr(provider, "last_error_detail"):
            telemetry["provider_error_detail"] = (
                str(getattr(provider, "last_error_detail", "") or "")[:200]
            )
        return telemetry

    def _native_event_cursor(self) -> int | None:
        cursor = getattr(self.graph, "native_event_cursor", None)
        if not callable(cursor):
            return None
        try:
            return int(cursor())
        except (TypeError, ValueError, OSError):
            return None

    def _native_observations_since(
        self,
        job: WorkerJob,
        cursor: int | None,
    ) -> list[SafeNativeObservation]:
        if cursor is None:
            return []
        read_events = getattr(self.graph, "native_fact_events_since", None)
        if not callable(read_events):
            return []
        try:
            events = read_events(cursor)
        except (OSError, RuntimeError, TypeError, ValueError):
            return []
        payload = dict(getattr(job, "payload", {}) or {})
        expected_action = str(payload.get("tool_name") or payload.get("tool") or "")
        return extract_native_observations(
            events,
            intent_id=str(job.intent_id or ""),
            expected_action=expected_action,
        )

    def _record_official_evidence(
        self,
        job: WorkerJob,
        result: OfficialWorkerResult,
        *,
        native_cursor: int | None = None,
    ) -> None:
        """Persist only a safe observation summary and durable Evidence refs.

        Native Worker output is intentionally excluded.  First consume the
        official Blackboard's structured fact product.  The injected bridge is
        the authority for Evidence references; the projection makes the safe
        fields visible to the next Coordinator/Reason turn and links them to
        the claimed official intent.  A generic fact remains the fail-closed
        fallback when the Worker did not produce a structured product.
        """

        refs = tuple(str(ref) for ref in result.evidence_refs if str(ref))
        if not refs:
            return
        observations = self._native_observations_since(job, native_cursor)
        if observations:
            for observation in observations:
                self._record_native_observation(job, observation, refs)
            return
        add_fact = getattr(self.graph, "add_fact", None)
        if not callable(add_fact):
            return
        intent_id = str(job.intent_id or job.worker_id)
        attempt_feedback = _safe_unverified_attempt(job)
        if attempt_feedback is not None:
            add_fact(
                actor=job.worker_id,
                content=json.dumps(
                    attempt_feedback,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                verified=False,
                evidence_refs=list(refs),
                dedupe_key=(
                    f"muteki-native-attempt:{intent_id}:"
                    f"{attempt_feedback['tool']}"
                ),
            )
            return
        add_fact(
            actor=job.worker_id,
            content="Native Worker completed a bounded evidence-backed observation",
            verified=False,
            evidence_refs=list(refs),
            dedupe_key=f"muteki-native-worker:{intent_id}:{','.join(refs)}",
        )

    def _record_native_observation(
        self,
        job: WorkerJob,
        observation: SafeNativeObservation,
        refs: tuple[str, ...],
    ) -> None:
        content = json.dumps(
            observation.payload(evidence_refs=refs),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        add_native_fact = getattr(self.graph, "add_native_fact", None)
        if callable(add_native_fact):
            add_native_fact(
                actor=job.worker_id,
                content=content,
                evidence_refs=list(refs),
                intent_id=job.intent_id,
            )
            return
        add_fact = getattr(self.graph, "add_fact", None)
        if callable(add_fact):
            add_fact(
                actor=job.worker_id,
                content=content,
                verified=False,
                evidence_refs=list(refs),
                dedupe_key=(
                    f"muteki-native-observation:{job.intent_id or job.worker_id}:"
                    f"{observation.fact_sequence}"
                ),
            )

    def _conclude_official_intent(self, job: WorkerJob, result: str) -> None:
        """Close a native intent without promoting its observation to a finding."""

        if not job.intent_id:
            return
        conclude_intent = getattr(self.graph, "conclude_intent", None)
        if callable(conclude_intent):
            conclude_intent(actor=job.worker_id, intent_id=job.intent_id, result=str(result))

    def _worker_step_tracker(self) -> tuple[dict[str, dict[str, Any]], threading.Lock]:
        """Return the per-Worker coalescing state, creating it on first use.

        Lazily created so a Coordinator assembled without ``__init__`` (the
        focused ``object.__new__`` style this suite uses) still records steps
        instead of raising on a missing attribute.
        """

        state = getattr(self, "_worker_step_state", None)
        lock = getattr(self, "_worker_step_lock", None)
        if state is None:
            state = {}
            self._worker_step_state = state
        if lock is None:
            lock = threading.Lock()
            self._worker_step_lock = lock
        return state, lock

    def _bind_runtime_loop(self) -> None:
        """Remember the loop the Coordinator itself runs on.

        Needed because Worker step callbacks arrive from a Worker thread, and
        that thread owns its own short-lived loop (``asyncio.run`` in
        ``_execute_openai_compatible_sync`` / ``execute``).  See
        :meth:`_publish_worker_step` for why that matters.
        """

        try:
            self._runtime_loop_ref = asyncio.get_running_loop()
        except RuntimeError:
            pass

    def _publish_worker_step(
        self,
        actor: str,
        payload: dict[str, Any],
        *,
        event_type: EventType = EventType.WORKER_STEP,
    ) -> None:
        """Emit one WORKER_STEP (or WORKER_COMMAND) onto the persisting loop.

        ``UpstreamRuntimeGraph.emit_event`` fans out to the audit bridge through
        ``EventBridge.callback().submit()``, which schedules the write as a task
        on **whatever loop is running in the calling thread**.  A Worker reports
        steps while a short-lived loop is active, so that task belonged to a loop
        that is torn down the moment the Worker returns -- the event was
        cancelled and silently lost.  (Events the Coordinator itself emits, e.g.
        ``phase_changed``, always persisted: they are already on the runtime
        loop.  That asymmetry is what identified this.)

        The CLI/container Worker is worse than that: ``CliSolver`` runs the
        subprocess in an ``asyncio.to_thread`` thread, so ``on_step`` fires with
        *no* loop at all.  ``EventBridge.submit`` then buffers the envelope, and
        the buffer is only drained by ``flush()`` at Run teardown -- the Worker
        looked frozen for its whole turn and spat out every step in one second
        at the end.

        Marshal onto the runtime loop instead, so observability is not a function
        of which thread happened to emit.  Observability is never worth failing a
        Worker over, so every failure path degrades to the direct call or to
        silence.
        """

        emit_event = getattr(self.graph, "emit_event", None)
        if not callable(emit_event):
            return

        async def _emit() -> None:
            emit_event(actor=actor, event_type=event_type, payload=payload)

        loop = getattr(self, "_runtime_loop_ref", None)
        if loop is not None and loop.is_running():
            try:
                caller_loop: Any = asyncio.get_running_loop()
            except RuntimeError:
                # A thread with NO loop at all is the *main* case to marshal,
                # not a reason to emit locally.  The CLI/container Worker runs
                # its subprocess through ``asyncio.to_thread`` inside
                # ``CliSolver._run_streaming``, so its ``on_step`` callbacks
                # arrive here from a plain worker thread.  ``EventBridge.submit``
                # buffers whenever it cannot see a running loop, so emitting
                # directly stranded *every* step of the round until
                # ``flush()`` at Run teardown -- the UI showed a 13-minute
                # frozen Worker and then 60+ steps in one second.
                caller_loop = None
            except Exception:
                caller_loop = None
            if caller_loop is not loop:
                try:
                    scheduled = asyncio.run_coroutine_threadsafe(_emit(), loop)
                    # Surface nothing, but consume the exception so a failed
                    # write never becomes "Future exception was never retrieved".
                    scheduled.add_done_callback(_discard_future_error)
                    return
                except Exception:
                    pass
        try:
            emit_event(actor=actor, event_type=event_type, payload=payload)
        except Exception:
            pass

    @property
    def _stagnation_lock(self) -> Any:
        """Lazily created per-instance lock.

        A ``threading.Lock`` cannot be a class attribute: every Coordinator in
        the process would share it.  Created on first use because
        ``object.__new__``-built Coordinators in tests never run ``__init__``.
        """

        existing = self.__dict__.get("_stagnation_lock_ref")
        if existing is not None:
            return existing
        lock = threading.Lock()
        self.__dict__["_stagnation_lock_ref"] = lock
        return lock

    def _command_fingerprint(self, command: str) -> str | None:
        """A short identity for "the same command", or ``None`` to opt out.

        Deliberately crude: the leading characters after whitespace normalisation.

        A real similarity measure (token overlap, normalised edit distance) would
        also flag two *different* probes of the same endpoint as "repeats" -- and
        varying a parameter across a small set is exactly what a working probe
        looks like (``id=1``, ``id=2``, ``id=1'``).  The loop this counter exists
        for is the degenerate case: byte-identical re-sends, seen 70 times in one
        Run.  Those are caught by a prefix; the legitimate variation is not.

        ``None`` for a command too short to carry identity, so a stream of tiny
        housekeeping commands (``ls``, ``pwd``) cannot trip the repeat counter.
        """

        config = getattr(self, "config", None)
        width = int(
            getattr(config, "stagnation_repeat_fingerprint_chars", 60) or 60
        )
        flat = " ".join((command or "").split())
        if len(flat) < 20:
            return None
        return flat[:width]

    def _stagnation_limit_for_progress(self) -> int:
        """The commands-since-progress allowance, grown by demonstrated progress.

        Flat ``stagnation_command_limit`` is the right budget for a Run that has
        not shown it can learn anything: it stops a Worker looping on one
        endpoint after 40 commands, which is the failure it was calibrated
        against.

        It is the *wrong* budget for a Run that is climbing a multi-stage chain,
        because the steps between stages are invisible to the fact counter by
        construction.  Deriving a filename oracle, brute-forcing an md5, or
        compiling a shared object are each dozens of commands that produce no
        new fact until they succeed -- indistinguishable, to a flat counter, from
        flailing.  Measured 2026-11-08: 33 of the 46 UNSOLVED non-CVE Web
        Challenges had already produced >=2 real conclusions before they were
        stopped; the exemplary one had the admin password, a confirmed upload and
        a solved filename oracle, and died mid-ELF-construction.

        So the allowance grows with the number of facts the Run has *already*
        produced -- progress buys runway, and nothing else does.  A Run with no
        facts keeps the flat value exactly, so the fast-fail path (and therefore
        the already-good CVE solve rate) is byte-identical.

        Read defensively throughout: this is called from the Worker's thread on a
        callback path that must never raise, and test doubles are built with
        ``object.__new__``.
        """

        config = getattr(self, "config", None)
        base = int(getattr(config, "stagnation_command_limit", 0) or 0) if config else 0
        if base <= 0:
            return base

        per_fact = int(
            getattr(config, "stagnation_progress_bonus_per_fact", 0) or 0
        )
        min_facts = int(
            getattr(config, "stagnation_progress_bonus_min_facts", 0) or 0
        )
        ceiling = int(getattr(config, "stagnation_command_limit_max", 0) or 0)
        if per_fact <= 0 or ceiling <= base:
            return base

        # Cached: this runs once per Worker command, and ``graph.facts()`` is a
        # SQLite query.  A short TTL rather than an event-driven invalidation,
        # deliberately: facts are written by the Worker's own thread (the
        # ``VERIFIED_FACT=`` recorder), which is this same hot path, so there is
        # no single place to hook a write.  The budget only needs to be roughly
        # right -- being 2 seconds stale can never let a Run overrun by more than
        # one command -- and a TTL cannot silently pin a stale value forever the
        # way a missed invalidation can.
        now = time.monotonic()
        cached = self.__dict__.get("_stagnation_fact_count")
        cached_at = float(self.__dict__.get("_stagnation_fact_count_at") or 0.0)
        if cached is None or (now - cached_at) > _STAGNATION_FACT_TTL_SECONDS:
            graph = getattr(self, "graph", None)
            try:
                cached = len(graph.facts()) if graph is not None else 0
            except Exception:
                cached = int(0 if cached is None else cached)
            self.__dict__["_stagnation_fact_count"] = cached
            self.__dict__["_stagnation_fact_count_at"] = now

        # Facts above the threshold are what earn runway.  ``min_facts <= 0``
        # means "no threshold" -- every fact counts -- rather than being silently
        # clamped to 1, which is what a ``max(1, ...)`` here would have done.
        eligible = max(0, int(cached) - max(0, min_facts))
        if eligible <= 0:
            return base
        return min(ceiling, base + per_fact * eligible)

    def _note_command_for_stagnation(self, command: str, result_excerpt: str) -> None:
        """Converge the Run when commands stop producing anything new.

        Why this exists, and why the existing limits do not cover it
        ------------------------------------------------------------
        ``barren_limit`` (8) counts **Worker rounds**, not commands.  Measured
        2026-11-06 across three runs, all three used only **two** Worker rounds:

        | run | commands | worker rounds | outcome |
        |---|---|---|---|
        | ``6712279f`` Joomla | 703 | 2 | UNSOLVED |
        | ``1b2a8586`` PHP | 74 | 2 | UNSOLVED |
        | ``e92b8431`` Drupal | 164 | 2 | SOLVED |

        So a Worker that goes in circles inside one round never accumulates
        barren rounds -- Joomla's 703 commands were spread over two rounds and
        the limit of 8 was never remotely in play.  ``worker_max_turns`` is no
        help either: a turn can issue many commands.

        The measurable shape of the failure is *command-level*: across all
        UNSOLVED runs, the last thing that counted as knowledge (a fact, a
        dead-end, a written finding) arrives at ~72% of the way through, and the
        remaining 28% of the command stream adds nothing.  On the PHP run the
        dead-end was written at command 26 of 36 and ten command were spent
        re-running the same ``find`` with a larger ``--max-time``.

        So the counter here is commands since the last new *fact*, not time and
        not rounds.  Hitting the threshold asks the Coordinator to converge --
        the same cooperative mechanism the deadline uses -- rather than stopping
        outright: the in-flight round still gets to finish and write down what it
        learned, which is the whole point of having it be convergence.

        Thread safety: called from the Worker's thread.  ``request_convergence``
        sets an ``asyncio.Event``, which is not safe to touch off-loop, so the
        call is marshalled onto the runtime loop the same way
        ``_publish_worker_step`` does it.
        """

        # ``config`` is absent on Coordinators built with ``object.__new__`` in
        # tests that only exercise the step recorder -- and the step callback is
        # exactly the path that must never raise, so read it defensively.
        config = getattr(self, "config", None)
        limit = self._stagnation_limit_for_progress()
        if limit <= 0 or getattr(self, "_stop_requested", False) or getattr(self, "_converging", False):
            return

        # ``result_excerpt`` is where a fact shows up: the Worker writes
        # ``VERIFIED_FACT=`` into its own shell output, and the recorder lifts
        # those into the graph.  Any of the markers means the stream is still
        # producing knowledge even though the command count is climbing.
        #
        # ``FLAG`` used to be in this list on its own, and that was a hole: a
        # Worker hunting a flag prints the word constantly -- ``find / -name
        # '*flag*'``, ``grep -r flag /var/www``, ``cat /flag`` -- so 17 of 154
        # results in run ``a6251f12`` contained it, each one resetting the counter.
        # That Run reached 296 commands with zero facts, which is exactly the
        # looping this counter exists to stop.  A real marker is ``FLAG=`` (a
        # value, not a noun) or a literal flag body; bare ``FLAG`` is prose.
        learned = any(
            marker in result_excerpt
            for marker in (
                "VERIFIED_FACT",
                "DEADEND",
                "FLAG=",
                "flag{",
                "FLAG{",
                "CTF{",
                "CTF2{",
            )
        )
        with self._stagnation_lock:
            # A *repeated* command is stagnation the fact counter cannot see, so
            # it is checked FIRST and independently.
            #
            # Measured 2026-11-09 on ``[BUUCTF 2018]Online Tool`` (run
            # ``af47e4d8``): 264 commands against a single target, of which 131
            # (~50%) shared their first 60 characters with another command -- one
            # ``curl -sS "http://<target>/...`` prefix appeared 70 times.  The Run
            # recorded ``VERIFIED_FACT`` markers throughout (every probe that
            # returned something produced a fact), so ``_commands_since_progress``
            # kept resetting and the 40-command budget never fired.  The Worker was
            # not learning; it was re-sending.
            #
            # Ordering is the whole point: the ``learned`` branch below returns
            # early, so a repeat check placed after it would only ever run on the
            # commands that already look unproductive -- precisely the ones that do
            # not need it.  Verified by the test that sends 5 identical commands
            # each carrying ``VERIFIED_FACT=``; the first version of this patch put
            # it after and that stream never converged.
            fingerprint = self._command_fingerprint(command)
            previous = self.__dict__.get("_last_command_fingerprint")
            self.__dict__["_last_command_fingerprint"] = fingerprint
            if fingerprint is not None and fingerprint == previous:
                self._repeated_commands = int(self.__dict__.get("_repeated_commands", 0)) + 1
            elif fingerprint is None:
                self._repeated_commands = 0
            else:
                # First sighting counts as one, so "5 identical commands in a row"
                # means five total, not one plus five repeats.  The off-by-one
                # mattered: with the counter starting at zero, ``REPEAT:5`` fired
                # on the sixth command, and the budget's own name said otherwise.
                self._repeated_commands = 1
            repeat_limit = int(getattr(config, "stagnation_repeat_limit", 0) or 0)
            repeated_out = repeat_limit > 0 and self._repeated_commands >= repeat_limit
            if repeated_out:
                self._repeated_commands = 0

            if learned:
                self._commands_since_progress = 0
                # A dead-end is not ordinary progress: it is the Worker
                # announcing that a direction is finished.  After it, the
                # tolerance for further commands should shrink, not reset.
                #
                # Measured on run ``1b2a8586`` (PHP CVE-2012-1823): the Worker
                # wrote ``mark-deadend 'Flag not retrievable'`` at command 26 of
                # 36 and then spent the next ten re-running the same ``find``
                # with ``--max-time`` raised from 40 to 90.  The prompt already
                # carried a "Stop condition (do not loop)" paragraph -- prose
                # does not bind, a counter does.
                if "DEADEND" in result_excerpt:
                    self._deadend_seen = True
                if not repeated_out:
                    return
                # A repeat is still a repeat even when the output looks like
                # knowledge -- that is the case this counter exists for.
                reached = True
                stalled_for = f"REPEAT:{repeat_limit}"
            elif repeated_out:
                self._commands_since_progress += 1
                reached = True
                stalled_for = f"REPEAT:{repeat_limit}"
            else:
                self._commands_since_progress += 1
                # Tighter budget once a direction has been written off.  The Worker
                # has already stated what does not work; whatever it tries next is
                # either genuinely new (which clears this on the next fact) or a
                # retry, and a retry should not get the full allowance again.
                after_deadend = int(
                    getattr(config, "stagnation_after_deadend_limit", 0) or 0
                )
                effective = (
                    min(limit, after_deadend)
                    if getattr(self, "_deadend_seen", False) and after_deadend > 0
                    else limit
                )
                reached = self._commands_since_progress >= effective
                stalled_for = self._commands_since_progress
                if reached:
                    # Reset so a second convergence request is not issued every
                    # command while the loop drains.
                    self._commands_since_progress = 0

        if not reached:
            return

        def _request() -> None:
            try:
                self.request_convergence(
                    f"STAGNATION:{stalled_for}", self._convergence_budget_seconds()
                )
            except Exception:
                pass

        loop = getattr(self, "_runtime_loop_ref", None)
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(_request)
                return
            except Exception:
                pass
        _request()

    def _record_worker_step(self, payload: dict[str, Any]) -> None:
        """Publish a coalesced WORKER_STEP so a Worker round is not a black box.

        Called from the Worker's thread for every parsed stream step.  Steps are
        folded into per-Worker aggregates and only published on a KIND change or
        once per ``WORKER_STEP_MIN_INTERVAL_SECONDS``; see those constants for
        why one event per line is the wrong shape.

        A step that carries ``command``/``result_excerpt`` is *also* published
        immediately as its own WORKER_COMMAND, **without** coalescing.  The
        aggregate only ever knew that "a shell tool ran"; after the fact nobody
        could tell which command produced the dead end, which is exactly what a
        post-mortem needs.  The text is already redacted by the Worker boundary
        (:mod:`muteki.redaction`); this method only bounds the volume.
        """

        worker_id = str(payload.get("worker_id") or "official-worker")
        kind = str(payload.get("kind") or "").strip()
        tool = str(payload.get("tool") or "").strip()
        session = str(payload.get("session") or "").strip()
        command = str(payload.get("command") or "").strip()
        result_excerpt = str(payload.get("result_excerpt") or "").strip()
        call_id = str(payload.get("call_id") or "").strip()
        now = time.monotonic()
        publish: dict[str, Any] | None = None
        snapshot: dict[str, Any] | None = None
        steps, lock = self._worker_step_tracker()
        with lock:
            state = steps.get(worker_id)
            if state is None:
                state = {
                    "first_emit": True,
                    "last_emit": now,
                    "last_kind": kind,
                    "pending": 0,
                    "steps": 0,
                    "tool_steps": 0,
                    "tools": [],
                    "session": session,
                    "commands": 0,
                }
                steps[worker_id] = state
            state["steps"] += 1
            state["pending"] += 1
            if session:
                state["session"] = session
            if kind == "tool":
                state["tool_steps"] += 1
                if tool and tool not in state["tools"] and len(state["tools"]) < WORKER_STEP_MAX_TOOLS:
                    state["tools"].append(tool)
            if (command or result_excerpt) and int(state.get("commands") or 0) < WORKER_COMMAND_MAX_PER_WORKER:
                state["commands"] = int(state.get("commands") or 0) + 1
                publish = {
                    "worker_id": worker_id,
                    "kind": kind,
                    "tool": tool,
                    "call_id": call_id,
                    "session": str(state["session"] or ""),
                    "turn": int(state["tool_steps"]),
                    "command": command,
                    "result_excerpt": result_excerpt,
                }
            kind_changed = bool(kind) and kind != state["last_kind"]
            due = (now - float(state["last_emit"])) >= WORKER_STEP_MIN_INTERVAL_SECONDS
            emit_snapshot = bool(state["first_emit"] or kind_changed or due)
            if emit_snapshot:
                state["first_emit"] = False
                state["last_emit"] = now
                if kind:
                    state["last_kind"] = kind
                snapshot = {
                    "worker_id": worker_id,
                    "kind": kind,
                    "tool": tool,
                    "session": str(state["session"] or ""),
                    "steps_total": int(state["steps"]),
                    "steps_in_window": int(state["pending"]),
                    "tool_steps": int(state["tool_steps"]),
                    "distinct_tools": list(state["tools"]),
                }
                state["pending"] = 0
        if publish is not None:
            self._publish_worker_step(
                worker_id, publish, event_type=EventType.WORKER_COMMAND
            )
            self._note_command_for_stagnation(command, result_excerpt)
        if snapshot is not None:
            self._publish_worker_step(worker_id, snapshot)

    def _flush_worker_steps(
        self, worker_id: str, *, result: str = "", result_code: str = ""
    ) -> None:
        """Publish the trailing step aggregate for a Worker that just returned.

        Without this the last (up to) ``WORKER_STEP_MIN_INTERVAL_SECONDS`` of a
        Worker round — often exactly the part that failed — would be coalesced
        away.  Idempotent: the per-Worker state is dropped once flushed.

        ``result_code`` is the Worker's own named outcome (e.g.
        ``OPENAI_COMPATIBLE_PROVIDER_DENIED``).  ``result`` alone collapses every
        failure into ``FAILED``, which is why a zero-step round used to be
        indistinguishable from a model that simply declined to call a tool.
        """

        key = str(worker_id or "")
        if not key:
            return
        steps, lock = self._worker_step_tracker()
        with lock:
            state = steps.pop(key, None)
        if state is None:
            return
        self._publish_worker_step(
            key,
            {
                "worker_id": key,
                "kind": "worker_finished",
                "tool": "",
                "session": str(state.get("session") or ""),
                "steps_total": int(state.get("steps") or 0),
                "steps_in_window": int(state.get("pending") or 0),
                "tool_steps": int(state.get("tool_steps") or 0),
                "distinct_tools": list(state.get("tools") or []),
                "worker_result": str(result or ""),
                "worker_result_code": str(result_code or ""),
            },
        )

    def request_convergence(
        self,
        reason: str = "CONVERGING",
        budget_seconds: float | None = None,
    ) -> None:
        """Stop opening new directions; let the in-flight Worker round finish.

        This is the soft half of a Run deadline.  A hard stop abandons whatever
        the current Worker round was about to verify; convergence instead gives
        that round its remaining time, which is what turns "stopped before
        verification completed" into "verification completed, then stopped".

        Idempotent, and deliberately weaker than ``request_stop``: a Run that
        is already stopping is not downgraded back into convergence.
        """

        if self._stop_requested:
            return
        self._converging = True
        self._converge_reason = str(reason or "CONVERGING")[:120]
        # The budget is a *whole-Run* allowance, not a per-wait one: the poll
        # loop and the Finalize fallback share this deadline, so a Run cannot
        # spend it twice and blow past the Runtime's Finalize grace.
        if self._converge_deadline is None:
            # The caller's budget is the authority when it is smaller: the
            # Runtime knows how much of the deadline it actually set aside,
            # and Finalize must not out-wait that slice and blow its own grace.
            budget = self._convergence_budget_seconds()
            if budget_seconds is not None:
                try:
                    budget = min(budget, max(0.0, float(budget_seconds)))
                except (TypeError, ValueError):
                    pass
            self._converge_started = time.monotonic()
            self._converge_deadline = self._converge_started + budget
            self._converged_rounds = 0
        # Deliberately a *separate* event from the stop wake-up.  The
        # convergence branch itself waits on stop, so raising convergence
        # through the same door would immediately satisfy its own wait and
        # turn the branch into a busy loop.
        wakeup = self._converge_wakeup
        if wakeup is not None:
            wakeup.set()

    def request_stop(self, reason: str = "STOP_REQUESTED") -> None:
        self._stop_requested = True
        self._stop_reason = str(reason or "STOP_REQUESTED")[:120]
        # Wake the in-flight poll/Worker wait so the stop is observed in
        # seconds instead of at the next tick boundary.
        wakeup = self._stop_wakeup
        if wakeup is not None:
            wakeup.set()

    async def send_control(self, worker_id: str, command: str, payload: dict | None = None) -> None:
        """Forward an operator command when a control transport is configured."""

        if self.control_receiver is None:
            raise RuntimeError("Muteki control receiver is not configured")
        await self.control_receiver.send_command(worker_id, command, payload)

    async def broadcast_control(self, command: str, payload: dict | None = None) -> None:
        if self.control_receiver is None:
            raise RuntimeError("Muteki control receiver is not configured")
        await self.control_receiver.broadcast(command, payload)

    def _close_active_intents(
        self,
        final_reason: str,
        *,
        emit_event: bool = True,
    ) -> dict[str, int | str]:
        """Close every active Intent before the Run becomes terminal.

        This is deliberately synchronous and runs before the first await in
        ``finalize`` so a cancelled/timeout task still leaves the Blackboard
        with an explicit terminal state for every active direction.

        Ordering is the point.  ``MutekiGraph.conclude_intent`` guards its
        UPDATE with ``status='claimed'``, so concluding an ``open`` Intent
        matches zero rows and leaves it ``open`` forever -- which reads in the
        UI as a "待验证" direction that was abandoned the moment the Run ended.
        A Run that is about to time out must therefore *claim* each pending
        direction before it concludes it, and every direction that still
        cannot be closed is reported explicitly instead of silently dropping
        out of the count.

        Convergence priority:

        1. ``claimed`` -- already mid-verification; conclude to release the
           lease and land a terminal state.
        2. ``open`` -- unclaimed; claim as the Coordinator, then conclude.
        3. anything else active -- best effort; a direction that matches no
           transition is surfaced through ``unclosed_intents``.
        """

        intents_reader = getattr(self.graph, "intents", None)
        claim_intent = getattr(self.graph, "claim_intent", None)
        conclude_intent = getattr(self.graph, "conclude_intent", None)
        if not callable(intents_reader):
            return {
                "active_intents": 0,
                "closed_intents": 0,
                "open_intents": 0,
                "claimed_intents": 0,
                "claimed_during_finalize": 0,
                "unclosed_intents": 0,
                "intent_result": "none",
            }
        try:
            intents = list(intents_reader())
        except Exception:
            intents = []
        active = [
            item
            for item in intents
            if str(getattr(item, "status", "") or "").casefold()
            in _ACTIVE_INTENT_STATUSES
        ]
        # Claimed directions already hold a lease, so retiring them first makes
        # the pending ones the only work left when the deadline is tight.
        active.sort(
            key=lambda item: 0
            if str(getattr(item, "status", "") or "").casefold() == "claimed"
            else 1
        )
        result = "timed_out" if "TIMEOUT" in str(final_reason).upper() else "cancelled"
        closed = 0
        claimed_here = 0
        for item in active:
            intent_id = str(getattr(item, "intent_id", "") or "")
            if not intent_id:
                continue
            status = str(getattr(item, "status", "") or "").casefold()
            if status != "claimed" and callable(claim_intent):
                try:
                    if claim_intent(
                        worker="coordinator",
                        intent_id=intent_id,
                        lease_s=FINALIZE_INTENT_LEASE_SECONDS,
                    ):
                        claimed_here += 1
                except Exception:
                    pass
            if not callable(conclude_intent):
                continue
            try:
                if conclude_intent(
                    actor="coordinator",
                    intent_id=intent_id,
                    result=result,
                ):
                    closed += 1
            except Exception:
                continue
        open_count = sum(
            1
            for item in active
            if str(getattr(item, "status", "") or "").casefold() == "open"
        )
        summary: dict[str, int | str] = {
            "active_intents": len(active),
            "closed_intents": closed,
            "open_intents": open_count,
            "claimed_intents": len(active) - open_count,
            "claimed_during_finalize": claimed_here,
            "unclosed_intents": len(active) - closed,
            "intent_result": result,
        }
        if emit_event:
            try:
                self.graph.emit_event(
                    actor="coordinator",
                    event_type=EventType.COORDINATOR_DIRECTIVE,
                    payload={
                        "directive": "finalize_active_intents",
                        "reason_code": str(final_reason)[:120],
                        **summary,
                    },
                )
            except Exception:
                pass
        return summary

    async def _await_stop_convergence(self, final_reason: str) -> dict[str, int | float]:
        """Let an in-flight Worker round finish before the Board is swept.

        The duration is not a fixed sleep -- it is decided by the round.  The
        wait ends the instant the pool drains (verification landed, or the round
        gave up), and only its ceiling comes from
        ``CoordinatorConfig.stop_convergence_seconds``.  A Run whose Workers
        already finished therefore pays nothing, while a Run that is one tool
        call away from a verified fact no longer throws that call away merely
        because the deadline fired first.

        This deliberately does NOT fabricate verification.  It cannot mark a
        candidate fact verified; it only stops Finalize from reaping a round
        that was still producing evidence.  Whatever the round proves is what
        lands, and whatever it does not prove stays a candidate.
        """

        budget = self._convergence_budget_seconds()
        started = time.monotonic()
        summary: dict[str, int | float] = {
            "convergence_budget_seconds": round(budget, 3),
            "convergence_waited_seconds": 0.0,
            "convergence_rounds": 0,
            "convergence_verified_delta": 0,
        }
        # The window only exists if this Run was actually granted one.  A bare
        # stop (operator cancel, barren limit, an already-expired deadline that
        # never asked to converge) must stay fast: granting it the full budget
        # here would make "stop" mean "stop, plus two more minutes", which is
        # the opposite of what a stop is for.  Run deadlines reach this method
        # with the deadline already set, because the Runtime requests
        # convergence before it stops.
        deadline = self._converge_deadline
        if deadline is None or budget <= 0:
            return summary
        started_at = getattr(self, "_converge_started", None)
        if not self.pool.active_count:
            # The poll loop already drained the pool: the verification landed
            # before Finalize.  Still report the window that was granted and
            # how much of it the round actually used, otherwise the audit trail
            # cannot tell "converged and finished" from "never asked to".
            loop_rounds = int(getattr(self, "_converged_rounds", 0) or 0)
            if loop_rounds or started_at is not None:
                summary["convergence_rounds"] = loop_rounds
                if started_at is not None:
                    summary["convergence_waited_seconds"] = round(
                        min(budget, max(0.0, time.monotonic() - started_at)), 3
                    )
                self._emit_convergence_directive(final_reason, summary)
            return summary
        try:
            verified_before = len(self.graph.flags(verified_only=True))
        except Exception:
            verified_before = 0
        rounds = int(getattr(self, "_converged_rounds", 0) or 0)
        while self.pool.active_count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(self.pool.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                # Budget exhausted mid-round.  The round keeps running and the
                # teardown below owns it, exactly as before.
                break
            except asyncio.CancelledError:
                raise
            except Exception:
                break
            rounds += 1
            if self.graph.flags(verified_only=True):
                # Already solved; there is nothing left to converge toward.
                break
        summary["convergence_rounds"] = rounds
        summary["convergence_waited_seconds"] = round(time.monotonic() - started, 3)
        try:
            summary["convergence_verified_delta"] = max(
                0, len(self.graph.flags(verified_only=True)) - verified_before
            )
        except Exception:
            pass
        self._emit_convergence_directive(final_reason, summary)
        return summary

    def _emit_convergence_directive(
        self,
        final_reason: str,
        summary: Mapping[str, object],
    ) -> None:
        """Publish what the convergence window actually bought, when it bought
        anything.  A Run that was never granted a window emits nothing: silence
        is the correct record for "no window was needed".  Any granted window is
        recorded even when it was brief -- "the deadline waited for the round"
        is exactly the fact a timeout audit needs to see."""

        if not (
            summary.get("convergence_rounds")
            or float(summary.get("convergence_waited_seconds") or 0.0) > 0.0
        ):
            return
        try:
            self.graph.emit_event(
                actor="coordinator",
                event_type=EventType.COORDINATOR_DIRECTIVE,
                payload={
                    "directive": "stop_convergence",
                    "reason_code": str(final_reason)[:120],
                    **summary,
                },
            )
        except Exception:
            pass

    async def finalize(self, *, reason: str) -> None:
        """Close the Run with exactly one durable terminal event.

        A cancellation can be delivered while Finalize is already running: a
        Run-level deadline, an operator cancel or a Supervisor grace expiry can
        land on any await below.  Each asynchronous cleanup step therefore runs
        through ``_terminal_step``, which keeps the step alive and consumes the
        re-entrant cancellation so the handoff can still land.  The terminal
        event is published only after every step completed, and ``_finalized``
        is set only by that publish, so "marked finalized but the event was
        never sent" is not reachable: an interrupted Finalize either raises or
        is simply still un-finalized.
        """

        # ``_finalizing`` is read defensively: focused tests construct the
        # Coordinator with ``object.__new__`` and set only the fields Finalize
        # reads, so the guard must not depend on ``__init__`` having run.
        if self._finalized or getattr(self, "_finalizing", False):
            return
        self._finalizing = True
        try:
            self._change_phase(MutekiPhase.FINALIZE)
            final_reason = self._stop_reason or reason
            # Spend the convergence budget BEFORE the sweep.  Any path into
            # Finalize that did not already drain the pool through the loop's
            # convergence branch (operator cancel, barren limit, race hand-off)
            # gets the same treatment here: an in-flight round finishes, then
            # the Board is swept.  Sweeping first would classify a round that
            # was still verifying as an abandoned direction.
            convergence = await self._await_stop_convergence(final_reason)
            # First sweep, still synchronous: it closes every direction the
            # converged Board holds before any teardown await runs, so a Run
            # cancelled during the reap still leaves an explicit terminal
            # state behind for every direction it had decided.
            intent_closure = self._close_active_intents(final_reason)
            # Cancel the native official Worker first.  Cancelling the pool
            # task first only cancels the asyncio wrapper; a CLI/RCP process
            # running in ``to_thread`` can then outlive the run and write late
            # Blackboard facts.  Official Muteki reaps the execution window
            # before retiring its task; keep that ordering at the integration
            # boundary too.
            if self.official_worker is not None:
                cancel_active = getattr(self.official_worker, "cancel_active", None)
                if callable(cancel_active):
                    await self._terminal_step(cancel_active())
            for task in (self._health_retry_task, self._reason_health_retry_task):
                if task is not None and not task.done():
                    task.cancel()
            await self._terminal_step(self._drain_health_retry_tasks())
            await self._terminal_step(self.pool.cancel_all())
            self._release_completed_semantics()
            if self.official_worker is not None:
                await self._terminal_step(self.official_worker.stop())
            # Second sweep, after the Worker is reaped: a Sandbox Worker writes
            # the shared graph from its own container, so it can land a brand
            # new (still ``open``) direction between the first sweep and the
            # teardown above.  Re-running here closes those too, and the
            # terminal payload reports both sweeps so a direction that was
            # claimed purely to retire it stays visible in the audit trail.
            late_closure = self._close_active_intents(final_reason, emit_event=False)
            if late_closure["active_intents"]:
                merged = dict(late_closure)
                for key in (
                    "active_intents",
                    "closed_intents",
                    "claimed_during_finalize",
                ):
                    merged[key] = int(late_closure[key]) + int(intent_closure[key])
                merged["unclosed_intents"] = int(merged["active_intents"]) - int(
                    merged["closed_intents"]
                )
                intent_closure = merged
            # The convergence window is part of the terminal record: it is the
            # only place the audit trail can say "the round we stopped for
            # actually finished, and here is what it added".
            if convergence.get("convergence_rounds") or convergence.get(
                "convergence_waited_seconds"
            ):
                intent_closure = {**intent_closure, **convergence}
            # Last act before the terminal event: if the clock ran out and no
            # verified flag exists, try the values the Workers already
            # recovered, re-wrapped under this challenge's own banner.  This
            # runs after convergence and both sweeps precisely because it must
            # see the *settled* board -- and because a Run with time left should
            # keep hunting for the real wrapper instead of submitting a
            # synthesised one.
            recovered = await self._recover_flag_from_evidence(final_reason)
            if recovered:
                final_reason = "FLAG_VERIFIED_BY_RECOVERY"
            self._publish_run_finished(final_reason, intent_closure)
        finally:
            self._finalizing = False

    async def _recover_flag_from_evidence(self, final_reason: str) -> bool:
        """Try the recovered values under this challenge's own banner.

        The arena tells us the banner up front (``flag_pattern``); only the box
        can tell us the value.  A Run that timed out holding one but not the
        other has both halves of the answer in different places, so the two are
        assembled and offered to the **same** gate as any other candidate.

        Measured on the real-class boxes (``0ab61266`` / ``879d42b0`` /
        ``87e96c2e``): all three ended holding a fact whose inner value was the
        genuine article wrapped in the box's own ``n1book{...}`` banner, and all
        three closed UNSOLVED with the value discarded.  The banner those Runs
        were missing is exactly the one this method already knows.

        Scope, deliberately narrow:

        * Only when the Run is *out of time* or otherwise closing unsolved --
          never while a round can still run.
        * Only when the board holds no verified flag already.
        * Never bypassing the gate: a candidate has to satisfy the pattern, pass
          the placeholder check, and appear in the recorded output.  A value the
          gate refuses stays a ``suspected`` candidate for review, which is the
          honest outcome -- it is not promoted to a solve.
        """

        if self.graph.flags(verified_only=True):
            return False
        if str(final_reason or "").upper().startswith("SOLVED"):
            return False
        # Every read below is optional: Finalize is also exercised by focused
        # tests whose fake graph implements only the methods that path uses, and
        # a recovery convenience must never be the reason a Run cannot close.
        lister = getattr(self.graph, "facts", None)
        if not callable(lister):
            return False
        pattern = str(getattr(self.graph, "flag_pattern", "") or "")
        if not pattern:
            config = getattr(self, "config", None)
            pattern = str(getattr(config, "flag_pattern", "") or "")
        from .recovery import Candidate, candidates_from_text, wrap_with_banner

        tried: set[str] = set()
        submitted: list[str] = []
        #: Every value recovered, whether or not the gate accepted it.  Kept so
        #: a refused value still reaches the review ledger.
        explicit: list = []
        # Sources, in order of how much they can be trusted:
        #   1. the candidates already harvested during the Run -- they are on the
        #      board, so Finalize does not depend on the facts still being active
        #      (the upstream backend drops concluded facts, and a Run usually
        #      concludes the very fact that carries its answer);
        #   2. the facts themselves, for a backend whose harvest never ran.
        suspect_reader = getattr(self.graph, "suspected_flags", None)
        if callable(suspect_reader):
            try:
                for item in suspect_reader() or ():
                    text_value = str(getattr(item, "flag_value", "") or "")
                    for candidate in candidates_from_text(text_value, source="candidate"):
                        if candidate not in explicit:
                            explicit.append(candidate)
            except Exception:  # noqa: BLE001 - a read convenience
                pass
        for fact in lister() or ():
            content = str(getattr(fact, "content", "") or "")
            fact_id = getattr(fact, "fact_id", "?")
            # The claim lives in ``content``; the *witness* has to come from what
            # the tools actually returned.  ``evidence_refs`` on these facts are
            # opaque ledger ids (``b5e147dd2ff5``), and the ledger's ``summary``
            # is a description ("Native Worker output retained..."), not the
            # output -- so neither can witness a value.
            #
            # ``result_excerpt`` is the retained tool output, and it is where the
            # recovered bytes really are: on run ``5e581902`` the base64 of
            # ``flag.php`` was in a ``php://filter`` result excerpt while the
            # fact only said "this is the flag".  Reading the excerpts is what
            # makes the check a witness rather than an echo.
            witness = await self._witness_for_fact(fact)
            if not witness:
                # No retained tool output anywhere: the only support for this
                # claim is the claim itself.  Refuse rather than accept a
                # self-witnessed value -- that is exactly the hallucination this
                # gate exists to stop.
                continue
            for candidate in candidates_from_text(content, source=f"fact:{fact_id}"):
                if candidate not in explicit:
                    explicit.append(candidate)
                if candidate.value and candidate.value not in witness:
                    # The value has to have been produced by the target.  A
                    # Worker that merely asserted it has no witness.
                    continue
                for spelling in wrap_with_banner(candidate, pattern):
                    if spelling in tried:
                        continue
                    tried.add(spelling)
                    if self._offer_recovered_flag(
                        spelling=spelling,
                        evidence=witness,
                        candidate=candidate,
                    ):
                        submitted.append(spelling)
        # Second pass: the candidates harvested during the Run.  They are tried
        # even when no fact survived, because on the ``upstream`` backend the
        # fact carrying the answer is usually concluded before Finalize -- so the
        # only durable record of the value is the candidate itself.  The witness
        # here is the retained output, fetched without a fact to key on.
        if not submitted and explicit:
            broad_witness = await self._broad_witness()
            if broad_witness:
                for candidate in explicit:
                    if candidate.value and candidate.value not in broad_witness:
                        continue
                    for spelling in wrap_with_banner(candidate, pattern):
                        if spelling in tried:
                            continue
                        tried.add(spelling)
                        if self._offer_recovered_flag(
                            spelling=spelling,
                            evidence=broad_witness,
                            candidate=candidate,
                        ):
                            submitted.append(spelling)
        if submitted:
            self.graph.emit_event(
                actor="coordinator",
                event_type=EventType.COORDINATOR_DIRECTIVE,
                payload={
                    "directive": "flag_recovered_from_evidence",
                    # The banner had to be supplied by us; say so, because a
                    # reviewer must be able to tell this apart from a Worker
                    # that read the real banner off the target.
                    "banner_supplied": True,
                    "candidates_tried": len(tried),
                    "flag": submitted[0],
                },
            )
            return True
        if tried:
            self.graph.add_dead_end(
                actor="coordinator",
                description=(
                    f"RECOVERY_CANDIDATES_REJECTED {len(tried)} value(s) under the "
                    "challenge banner did not satisfy the gate"
                ),
            )
        self._retain_candidates(explicit, tried)
        return False

    def _retain_candidates(self, explicit: list, tried: set[str]) -> None:
        """Keep every recovered value for a human, even when the gate refused it.

        A refusal means "this is not the declared banner", not "this is
        worthless": on the real-class boxes the value was genuine and only the
        wrapper came from somewhere else.  Dropping it loses the one piece of
        information the next run cannot cheaply re-derive.

        Both spellings are kept -- the value under the challenge banner (what we
        tried) and under the banner it arrived in (what the box actually said) --
        because a reviewer needs to see both to judge it.
        """

        recorder = getattr(self.graph, "record_flag_decision", None)
        if not callable(recorder):
            return
        for spelling in sorted(tried):
            try:
                recorder(
                    actor="coordinator:candidate",
                    decision=_RefusedCandidate(spelling),
                    evidence="recovered from retained tool output",
                )
            except Exception:  # noqa: BLE001 - bookkeeping only
                continue
        for candidate in explicit:
            if not candidate.banner:
                continue
            found = candidate.as_found
            if found in tried:
                continue
            try:
                recorder(
                    actor="coordinator:candidate",
                    decision=_RefusedCandidate(found),
                    evidence=f"as found wrapped in {candidate.banner}",
                )
            except Exception:  # noqa: BLE001
                continue

    async def _harvest_candidates(self) -> None:
        """Record flag-shaped values the retained output already contains.

        Runs after every Worker turn, so a value the box printed with its own
        banner becomes a durable candidate immediately instead of being dropped.
        It is *not* a submission: the gate still decides at Finalize whether the
        value can be accepted under this challenge's banner.  Recording early
        matters for two reasons:

        * the next Reason turn can read it from the board;
        * a Run that is cancelled rather than timed out still leaves the value in
          the ledger for a human, which a Finalize-only path would lose.

        Bounded per turn and never fatal -- this is bookkeeping beside the solve,
        not part of it.
        """

        recorder = getattr(self.graph, "record_flag_decision", None)
        if not callable(recorder):
            # Visible on purpose: on ``upstream`` this means the adapter lost the
            # method, and every harvest would silently do nothing.
            self.graph.add_dead_end(
                actor="coordinator",
                description="CANDIDATE_HARVEST_UNAVAILABLE graph has no record_flag_decision",
            )
            return
        lister = getattr(self.graph, "facts", None)
        if not callable(lister):
            return
        seen = getattr(self, "_harvested_values", None)
        if seen is None:
            seen = set()
            self._harvested_values = seen
        from .recovery import candidates_from_text

        harvested = 0
        for fact in lister() or ():
            content = str(getattr(fact, "content", "") or "")
            for candidate in candidates_from_text(content, source="harvest"):
                key = (candidate.banner, candidate.value)
                if key in seen:
                    continue
                seen.add(key)
                spelling = candidate.as_found if candidate.banner else candidate.value
                try:
                    recorder(
                        actor=f"coordinator:harvest:{candidate.banner or 'bare'}",
                        decision=_RefusedCandidate(spelling),
                        evidence=content[:400],
                    )
                except Exception:  # noqa: BLE001 - bookkeeping only
                    continue
                harvested += 1
                if harvested >= 20:  # per-turn ceiling
                    return

    async def _broad_witness(self) -> str:
        """All retained output for this Run, with no fact to key on.

        Needed because the fact that carries a recovered value is frequently
        concluded before Finalize runs, and the ``upstream`` backend drops
        concluded facts -- so keying the lookup on a fact loses the witness at
        exactly the moment it is needed.
        """

        reader = getattr(self, "_evidence_reader", None)
        if callable(reader):
            try:
                text = await reader(())
            except Exception:  # noqa: BLE001 - a read convenience
                text = ""
            if text:
                return str(text)
        lister = getattr(self.graph, "events_since", None)
        if not callable(lister):
            return ""
        try:
            events = lister(0) or ()
        except Exception:  # noqa: BLE001
            return ""
        parts: list[str] = []
        for event in events:
            if "worker_command" not in str(getattr(event, "event_type", "") or ""):
                continue
            payload = getattr(event, "payload", None) or {}
            if not isinstance(payload, dict):
                continue
            excerpt = str(payload.get("result_excerpt") or "")
            if excerpt:
                parts.append(excerpt)
        return "\n".join(parts)

    async def _witness_for_fact(self, fact) -> str:
        """The retained tool output that could witness this fact's claim.

        Prefers the reader injected by the Runtime (the platform's retained tool
        output, i.e. ``result_excerpt``); falls back to graph events only for
        backends that carry excerpts.  Returns ``""`` when neither source has
        anything, which the caller treats as "no witness" -- a fact whose only
        support is its own prose is not evidence.
        """

        reader = getattr(self, "_evidence_reader", None)
        if callable(reader):
            try:
                text = await reader(getattr(fact, "evidence_refs", ()) or ())
            except Exception:  # noqa: BLE001 - a read convenience
                text = ""
            if text:
                return str(text)

        lister = getattr(self.graph, "events_since", None)
        if not callable(lister):
            return ""
        refs = {str(ref) for ref in (getattr(fact, "evidence_refs", ()) or ())}
        own: list[str] = []
        other: list[str] = []
        try:
            events = lister(0) or ()
        except Exception:  # noqa: BLE001 - a read convenience
            return ""
        for event in events:
            kind = str(getattr(event, "event_type", "") or "")
            if "worker_command" not in kind:
                continue
            payload = getattr(event, "payload", None) or {}
            if not isinstance(payload, dict):
                continue
            excerpt = str(payload.get("result_excerpt") or "")
            if not excerpt:
                continue
            call_id = str(payload.get("call_id") or "")
            if refs and (call_id in refs or any(ref and ref in call_id for ref in refs)):
                own.append(excerpt)
            else:
                other.append(excerpt)
        if own:
            return "\n".join(own)
        return "\n".join(other)

    def _offer_recovered_flag(self, *, spelling: str, evidence: str, candidate) -> bool:
        """Submit one re-wrapped candidate to the platform, never to our own gate.

        Read this before changing it.  A value the box printed under its own
        banner, re-wrapped under the challenge's banner, is a **guess about the
        wrapper**.  Validating it locally proves only that the string has the
        right shape and that its inner value appeared in the output -- it cannot
        prove the platform agrees, because the arena mints flags per instance
        (``CTF2{<uuid>}``), so a genuine-looking ``CTF2{afr_1_solved}`` is
        rejected even though every local check passes.

        Measured on run ``de9ab053``: this method produced
        ``CTF2{afr_1_solved}``, the local gate said VALID, the Run closed
        ``COMPLETED_SOLVED``, and the platform answered ``rejected``.  A local
        "solved" that the arena does not honour is worse than an honest
        UNSOLVED, because it corrupts the success ledger.

        So: the value is recorded as a candidate for the submit path to try, and
        this method reports ``False`` -- it never declares a solve.  A Run closes
        solved only when a Worker read the flag the platform actually minted.
        """

        recorder = getattr(self.graph, "record_flag_decision", None)
        if not callable(recorder):
            return False
        try:
            recorder(
                actor=f"coordinator:recovery:{candidate.banner or 'bare'}",
                # Always the refused shape: a candidate to submit, not a solve.
                decision=_RefusedCandidate(spelling),
                evidence=evidence,
            )
        except Exception:  # noqa: BLE001 - recovery must never fail Finalize
            return False
        return False

    async def _drain_health_retry_tasks(self) -> None:
        """Wait for the per-Run health probes to acknowledge cancellation."""

        await asyncio.gather(
            *(
                task
                for task in (self._health_retry_task, self._reason_health_retry_task)
                if task is not None
            ),
            return_exceptions=True,
        )

    async def _terminal_step(self, awaitable) -> None:
        """Complete one Finalize step despite a re-entrant cancellation.

        ``asyncio.shield`` keeps the cleanup step alive when the Run task is
        cancelled again, and the delivered cancellation is consumed so the
        remaining terminal steps (and the terminal event) still run.  That
        absorption is bounded by ``FINALIZE_CANCEL_GRACE_SECONDS``: a step that
        is genuinely wedged must not make the Coordinator unreclaimable by the
        Runtime or Supervisor that is trying to abandon it, so the window
        expires with an explicit ``TimeoutError`` instead.  A failure or
        cancellation of the step itself is re-raised unchanged.
        """

        step = asyncio.ensure_future(awaitable)
        loop = asyncio.get_running_loop()
        deadline: float | None = None
        try:
            while True:
                timeout = None
                if deadline is not None:
                    timeout = deadline - loop.time()
                    if timeout <= 0:
                        raise asyncio.TimeoutError(
                            "Muteki Finalize step exceeded its cancellation grace"
                        )
                try:
                    if timeout is None:
                        await asyncio.shield(step)
                    else:
                        # Bounded so the absorption window cannot be extended
                        # indefinitely by a wedged step.
                        await asyncio.wait_for(
                            asyncio.shield(step),
                            timeout=timeout,
                        )
                except asyncio.CancelledError:
                    if step.done():
                        break
                    current = asyncio.current_task()
                    if current is not None:
                        # Only the await was cancelled; release the request so
                        # the remaining terminal steps are not immediately
                        # re-cancelled.
                        current.uncancel()
                    if deadline is None:
                        deadline = loop.time() + FINALIZE_CANCEL_GRACE_SECONDS
                    continue
                return
        finally:
            if not step.done():
                # Leave no orphan: a cancelled Task never reports its
                # CancelledError, so this cannot surface as a stray warning.
                step.cancel()
        step.result()

    def _publish_run_finished(
        self,
        final_reason: str,
        intent_closure: Mapping[str, object],
    ) -> None:
        """Publish the single terminal event, then mark the Run finalized.

        This is synchronous on purpose: no cancellation can be delivered while
        the handoff is written, and ``_finalized`` becomes observable only
        after the event exists.
        """

        try:
            self.graph.release_claims(actor="coordinator")
        except Exception:
            pass
        try:
            verified_flags = self.graph.flags(verified_only=True)
        except Exception:
            verified_flags = ()
        self.graph.emit_event(
            actor="coordinator",
            event_type=EventType.RUN_FINISHED,
            payload={
                "reason": final_reason,
                "flag_found": bool(verified_flags),
                "verified_flag_count": len(verified_flags),
                **intent_closure,
            },
        )
        self._finalized = True

    def _change_phase(self, phase: MutekiPhase) -> None:
        if self.phase is phase:
            return
        if not self.stage_policy.can_transition(self.phase, phase):
            raise ValueError(f"illegal Muteki stage transition: {self.phase} -> {phase}")
        self.phase = phase
        self.graph.emit_event(actor="coordinator", event_type=EventType.PHASE_CHANGED, payload={"phase": phase.value})


def _discard_future_error(future: Any) -> None:
    """Consume a fire-and-forget future's exception.

    ``_publish_worker_step`` never waits on the marshal: a step write must not
    block or fail a Worker.  Without a callback the exception would only surface
    as an unhandled-future warning at interpreter shutdown, which reads like a
    bug in the Worker instead of a dropped observability write.
    """

    try:
        future.exception()
    except Exception:
        pass


def _safe_worker_reason(value: object) -> str:
    """Return only an adapter status code for durable dead-end telemetry."""

    text = str(value or "").strip()
    if len(text) > 120 or not re.fullmatch(r"[A-Z0-9][A-Z0-9_.:-]*", text):
        return ""
    return text

def _safe_unverified_attempt(job: WorkerJob) -> dict[str, str] | None:
    """Return safe action identity when the Worker produced no typed result.

    This is execution feedback, not a vulnerability fact. It lets Strategy
    retire the exact attempted field/metadata route without treating the lack
    of a typed observation as a verified negative result.
    """

    payload = dict(getattr(job, "payload", {}) or {})
    tool = str(payload.get("tool_name") or payload.get("tool") or "").strip()
    if not tool or not all(
        char in "abcdefghijklmnopqrstuvwxyz0123456789_" for char in tool
    ):
        return None
    feedback: dict[str, str] = {"tool": tool, "observation_status": "UNAVAILABLE"}
    arguments = payload.get("arguments")
    if not isinstance(arguments, dict):
        return feedback
    if tool == "sql_boolean_compare":
        field = str(arguments.get("test_field") or "").strip()
        if field and all(
            char in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-"
            for char in field[:80]
        ):
            feedback["test_field"] = field[:80]
    elif tool == "mysql_metadata_discovery":
        stage = str(arguments.get("stage") or "").strip().lower()
        expression = str(arguments.get("target_expression") or "").strip()
        if stage in {"database", "tables", "columns"}:
            feedback["stage"] = stage
        if expression in {
            "DATABASE()",
            "information_schema.tables",
            "information_schema.columns",
        }:
            feedback["target_expression"] = expression
    return feedback


__all__ = ["CoordinatorConfig", "MutekiCoordinator"]
