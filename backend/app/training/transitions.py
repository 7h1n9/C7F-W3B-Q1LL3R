"""Build (state, action, observation, label) transitions from raw run rows.

Pure functions only: no ORM, no database, no I/O.  The export script feeds
plain dicts in, tests feed plain dicts in, and the output is plain dicts that
serialize straight to JSONL.  Keeping this module side-effect free is what
makes the dataset auditable -- the same input always yields the same output.

Event contract (measured against the live ``run_events`` table, 2026-11):

* ``muteki.worker_command`` payload:
  ``{kind, tool, turn, call_id, command, session, worker_id, result_excerpt}``
* ``muteki.fact_added`` / ``muteki.dead_end`` payload:
  ``{payload: {fact|reason|text, ...}, upstream_sequence, upstream_event_type}``

A transition is emitted per ``worker_command``.  Its ``state`` is a cheap
snapshot of what the agent had established *before* acting (fact / dead-end
counts plus a short window of recent commands).  Its ``observation`` carries
the command's own ``result_excerpt`` plus the knowledge deltas that landed
between this command and the next one -- that is the local reward signal later
stages optimise against, and it comes from the shared graph, not from the
model's self-report.
"""

from __future__ import annotations

import bisect
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.services.learned_skills import SensitiveValueSanitizer

# ---------------------------------------------------------------------------
# Event types and payload shapes
# ---------------------------------------------------------------------------

EVT_COMMAND = "muteki.worker_command"
EVT_FACT = "muteki.fact_added"
EVT_DEAD_END = "muteki.dead_end"
TRACKED_EVENTS = (EVT_COMMAND, EVT_FACT, EVT_DEAD_END)

#: A fact/dead-end event is bookkeeping, not evidence, when its actor carries
#: this prefix: the coordinator re-offers a recovered flag without the worker
#: having earned it, and counting it would inflate the "knowledge gained"
#: signal of runs that actually went nowhere.
RECOVERY_ACTOR_PREFIX = "coordinator:recovery:"

#: Terminal errors that mean "the harness failed", not "the agent failed".
#: Runs ending this way teach nothing about strategy and are excluded.
INFRA_ERROR_CODES = frozenset(
    {
        "STALLED_NO_WORKER_COMMAND",
        "WORKER_STALL_NO_COMMANDS",
        "NO_HEALTHY_WORKER",
        "NO_HEALTHY_WORKER_PROFILE",
        "WORKER_HEALTHCHECK_FAILED",
        "ARENA_MCP_UNREACHABLE",
        "ENVIRONMENT_LEASE_FAILED",
    }
)

#: Free-text needles matched case-insensitively against ``last_error_message``
#: for the older runs that predate structured error codes.
INFRA_ERROR_MESSAGE_NEEDLES = (
    "no healthy worker",
    "worker healthcheck",
    "arena mcp unreachable",
    "stalled_no_worker_command",
)

#: Statuses whose trajectories are meaningless as demonstrations.  Everything
#: else (SOLVED, UNSOLVED, TIMEOUT, FAILED...) is kept: a failed-but-honest
#: trajectory is exactly what dead-end mining needs.
EXCLUDED_STATUSES = frozenset({"CANCELLED", "CREATED", "QUEUED", "RUNNING"})

MAX_RECENT_COMMANDS = 5
COMMAND_CHARS = 400
EXCERPT_CHARS = 600
FACT_TEXT_CHARS = 300

_CVE_RE = re.compile(r"CVE-\d{4}-\d{4,7}", re.I)

#: Only the origin of a URL is sensitive: the host identifies the challenge
#: instance, while the path and query carry the *technique* (``/admin``,
#: ``?id=1``, ``/api/v2/users``).  The shared sanitiser replaces the whole URL,
#: which silently deletes exactly the part a skill candidate needs -- so the
#: origin is redacted here first, and the sanitiser then has nothing left to
#: match.
URL_ORIGIN_RE = re.compile(r"(?i)\bhttps?://[^/\s\"'<>]+")

#: Canonical technique paths.  The shared sanitiser's ``LINUX_PATH`` rule
#: rewrites ``/etc/passwd`` to ``{{linux_path}}`` -- it is meant for
#: machine-specific workspace paths, and applied to these it erases the single
#: most diagnostic string in any LFI walkthrough.  These are public knowledge,
#: not secrets, so they are stashed behind a token, the sanitiser runs, and
#: they are restored afterwards.
PROTECTED_PATHS: tuple[str, ...] = (
    "/etc/passwd",
    "/etc/shadow",
    "/etc/hosts",
    "/proc/self/environ",
    "/proc/self/cmdline",
    "/flag.txt",
    "/flag",
)

_PROTECT_TOKEN = "\x01p{}\x01"

#: Any CTF-style flag, not only the ``flag{...}`` prefix the shared sanitiser
#: knows about: arena challenges ship flags like ``DASCTF{...}``,
#: ``NSSCTF{...}`` or ``SCTF{...}``, and those are exactly the answers a
#: training set must not carry.
FLAG_ANY_RE = re.compile(r"(?i)\b[a-z0-9_]{0,24}(?:flag|ctf)\{[^{}\r\n]{1,120}\}")

#: A flag sliced in half by truncation has no closing brace, so the regex
#: above can never see it.  Redaction therefore runs *before* truncation, and
#: this tail rule catches whatever still looks like the head of a flag at the
#: end of a shortened field.
FLAG_TRUNCATED_TAIL_RE = re.compile(r"(?i)\b[a-z0-9_]{0,24}(?:flag|ctf)\{[^{}\r\n]{6,}$")

#: The same problem, anywhere in the field: a split-flag challenge prints
#: "PART ONE: flag{r0bots_..." and the two halves are never joined on the wire,
#: so no complete ``{...}`` exists to match.  A body of six or more characters
#: distinguishes that from the bare literal ``flag{`` used inside recon
#: commands (``grep -r "flag{" .``), which must stay readable.
FLAG_HEAD_RE = re.compile(r"(?i)\b[a-z0-9_]{0,24}(?:flag|ctf)\{[^{}\s\"']{6,}")


# ---------------------------------------------------------------------------
# Input records (plain dicts, see export script for the SQL projection)
# ---------------------------------------------------------------------------


@dataclass
class RunInput:
    """One ``solve_runs`` row plus its labels, as plain data."""

    run_id: str
    challenge_id: str
    status: str
    last_error_code: str = ""
    last_error_message: str = ""
    created_at: str = ""
    accepted: bool = False
    fresh_reproduction_verified: bool = False
    challenge_name: str = ""


@dataclass
class EventInput:
    """One ``run_events`` row.

    ``seq`` is assigned by the caller in true database order (``run_events``
    primary keys are UUIDs, so the exporter orders by ``created_at, id`` and
    then numbers the rows); this module only ever compares ``seq``.
    """

    seq: int
    event_type: str
    event_id: str = ""
    payload: dict = field(default_factory=dict)
    created_at: str = ""


# ---------------------------------------------------------------------------
# Quality gate
# ---------------------------------------------------------------------------


def classify_run(run: RunInput, command_count: int) -> str | None:
    """Return ``None`` when the run is trainable, else the exclusion reason.

    The reason string is part of the dataset's audit trail: every excluded run
    is still listed in the run-level sidecar file with this reason attached.
    """

    status = (run.status or "").upper()
    if status in EXCLUDED_STATUSES:
        return f"status:{status.lower()}"
    code = (run.last_error_code or "").upper()
    if code in INFRA_ERROR_CODES:
        return f"infrastructure:{code.lower()}"
    message = (run.last_error_message or "").lower()
    for needle in INFRA_ERROR_MESSAGE_NEEDLES:
        if needle in message:
            return f"infrastructure:{needle.replace(' ', '_')}"
    if command_count == 0:
        return "no_commands"
    return None


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------


def _nested_payload(event: EventInput) -> dict:
    """Unwrap the ``{payload: {...}, upstream_*}`` envelope when present."""

    payload = event.payload or {}
    inner = payload.get("payload")
    return inner if isinstance(inner, dict) else payload


def _event_actor(event: EventInput) -> str:
    actor = (event.payload or {}).get("actor")
    return actor if isinstance(actor, str) else ""


def _fact_text(event: EventInput) -> str:
    inner = _nested_payload(event)
    for key in ("fact", "reason", "text"):
        value = inner.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:FACT_TEXT_CHARS]
    return ""


def _is_countable_knowledge(event: EventInput) -> bool:
    return not _event_actor(event).startswith(RECOVERY_ACTOR_PREFIX)


def redact_public_text(text: str) -> str:
    """Apply the dataset's redaction rules to one string, without bookkeeping.

    Downstream stages (candidate mining, replay tooling) must redact *exactly*
    the same way the dataset was redacted, otherwise a shape meant to match a
    stored command silently stops matching.  Keeping one implementation here is
    the only way to guarantee that.
    """

    value = str(text or "")
    value = URL_ORIGIN_RE.sub("{{target_url}}", value)
    protected: dict[str, str] = {}
    for position, candidate in enumerate(PROTECTED_PATHS):
        if candidate in value:
            token = _PROTECT_TOKEN.format(position)
            protected[token] = candidate
            value = value.replace(candidate, token)
    value, _ = SensitiveValueSanitizer.sanitize(value)
    value = FLAG_ANY_RE.sub("{{flag_pattern}}", value)
    value = FLAG_TRUNCATED_TAIL_RE.sub("{{flag_pattern}}", value)
    value = FLAG_HEAD_RE.sub("{{flag_pattern}}", value)
    for token, original in protected.items():
        value = value.replace(token, original)
    return value


# ---------------------------------------------------------------------------
# Transition construction
# ---------------------------------------------------------------------------


def build_transitions(
    run: RunInput,
    events: Iterable[EventInput],
    known_secrets: Iterable[str] = (),
) -> tuple[list[dict], dict]:
    """Turn one run's events into transitions plus a run-level audit record.

    Returns ``(transitions, audit)``.  ``audit`` always exists -- even when
    ``transitions`` is empty -- so exclusion is visible rather than silent.

    ``known_secrets`` are literal strings that must never survive into the
    dataset -- in practice the platform's accepted flags, collected from
    ``arena_submissions``.  Exact replacement is the only reliable redaction
    for a flag whose prefix is unknown, and it also covers the ones cut short
    by field truncation.
    """

    ordered = sorted(
        (e for e in events if e.event_type in TRACKED_EVENTS),
        key=lambda e: e.seq,
    )
    commands = [e for e in ordered if e.event_type == EVT_COMMAND]
    command_seqs = [e.seq for e in commands]

    # Knowledge events between command i and command i+1 are the observable
    # effect of command i, so they are tallied into transition i's
    # observation.  Bucket them once, up front, with a single pass.
    deltas: list[dict[str, int]] = [
        {"facts": 0, "dead_ends": 0} for _ in commands
    ]
    fact_texts_by_command: list[list[str]] = [[] for _ in commands]
    for event in ordered:
        if event.event_type == EVT_COMMAND or not _is_countable_knowledge(event):
            continue
        slot = bisect.bisect_right(command_seqs, event.seq) - 1
        if slot < 0:
            continue
        if event.event_type == EVT_FACT:
            deltas[slot]["facts"] += 1
            text = _fact_text(event)
            if text:
                fact_texts_by_command[slot].append(text)
        elif event.event_type == EVT_DEAD_END:
            deltas[slot]["dead_ends"] += 1

    audit: dict[str, Any] = {
        "run_id": run.run_id,
        "challenge_id": run.challenge_id,
        "status": run.status,
        "accepted": run.accepted,
        "fresh_reproduction_verified": run.fresh_reproduction_verified,
        "command_events": len(commands),
        "fact_events": 0,
        "dead_end_events": 0,
        "redactions": 0,
        "exclude_reason": None,
    }

    reason = classify_run(run, command_count=len(commands))
    if reason is not None:
        audit["exclude_reason"] = reason
        return [], audit

    transitions: list[dict] = []
    recent_commands: list[str] = []
    facts_so_far = 0
    dead_ends_so_far = 0
    recent_fact_texts: list[str] = []

    #: Short literals are dropped: a 3-character "flag" would match half the
    #: corpus and destroy the field it is supposed to protect.
    secrets = tuple(s for s in known_secrets if isinstance(s, str) and len(s) >= 6)

    def redact(text: str, limit: int) -> str:
        """Redact, then truncate.  Never the other way round.

        Truncating first slices flags in half, and a half flag matches neither
        the exact-secret pass nor any ``{...}`` regex -- that is precisely how
        the first export leaked ``flag{Y0u_@re_3enset1ve_...`` into the data.
        """
        value = str(text or "")
        for secret in secrets:
            if secret in value:
                value = value.replace(secret, "{{flag_pattern}}")
                audit["redactions"] += 1
        # Origin first, so the shared sanitiser's whole-URL rule has no match
        # and the path survives into the dataset.
        value, count = URL_ORIGIN_RE.subn("{{target_url}}", value)
        audit["redactions"] += count
        # Stash canonical technique paths, then put them back after the
        # sanitiser has run (see PROTECTED_PATHS).
        protected: dict[str, str] = {}
        for position, candidate in enumerate(PROTECTED_PATHS):
            if candidate in value:
                token = _PROTECT_TOKEN.format(position)
                protected[token] = candidate
                value = value.replace(candidate, token)
        value, scan = SensitiveValueSanitizer.sanitize(value)
        audit["redactions"] += scan["redaction_count"]
        value, count = FLAG_ANY_RE.subn("{{flag_pattern}}", value)
        audit["redactions"] += count
        value, count = FLAG_TRUNCATED_TAIL_RE.subn("{{flag_pattern}}", value)
        audit["redactions"] += count
        value, count = FLAG_HEAD_RE.subn("{{flag_pattern}}", value)
        audit["redactions"] += count
        for token, original in protected.items():
            value = value.replace(token, original)
        return value[:limit]

    # Redact fact texts once, up front, so the redaction counter stays exact
    # even when a text is quoted into both an observation and a later state.
    sanitized_fact_texts: list[list[str]] = [
        [redact(t, FACT_TEXT_CHARS) for t in texts] for texts in fact_texts_by_command
    ]

    for index, event in enumerate(commands):
        # Newer workers write the flat shape; older ones used the same
        # ``{payload: {...}, upstream_*}`` envelope as knowledge events.
        body = _nested_payload(event)
        tool = str(body.get("tool") or "")
        worker_id = str(body.get("worker_id") or "")
        turn = body.get("turn")
        call_id = str(body.get("call_id") or "")

        transition = {
            "run_id": run.run_id,
            "challenge_id": run.challenge_id,
            "event_id": event.event_id,
            "sequence": index,
            "state": {
                "facts": facts_so_far,
                "dead_ends": dead_ends_so_far,
                "recent_facts": list(recent_fact_texts[-3:]),
                "recent_commands": list(recent_commands[-MAX_RECENT_COMMANDS:]),
                "commands_issued": index,
            },
            "action": {
                "tool": redact(tool, 80),
                "command": redact(body.get("command"), COMMAND_CHARS),
                "worker_id": worker_id,
                "turn": turn,
                "call_id": call_id,
            },
            "observation": {
                "result_excerpt": redact(body.get("result_excerpt"), EXCERPT_CHARS),
                "delta_facts": deltas[index]["facts"],
                "delta_dead_ends": deltas[index]["dead_ends"],
            },
            "labels": {
                "final_status": run.status,
                "accepted": run.accepted,
                "fresh_reproduction_verified": run.fresh_reproduction_verified,
            },
        }
        transitions.append(transition)
        recent_commands.append(transition["action"]["command"][:120])
        recent_fact_texts.extend(sanitized_fact_texts[index])
        facts_so_far += deltas[index]["facts"]
        dead_ends_so_far += deltas[index]["dead_ends"]

    audit["fact_events"] = facts_so_far
    audit["dead_end_events"] = dead_ends_so_far
    return transitions, audit


# ---------------------------------------------------------------------------
# Leakage-safe splitting
# ---------------------------------------------------------------------------


def group_key_for(challenge_id: str, challenge_name: str = "") -> str:
    """Return the split group for a challenge.

    Challenges that share a CVE share a group, so every run of a CVE family
    lands in the same split and the held-out set stays honest about
    generalisation to *unseen vulnerabilities*, not just unseen instances of
    the same one.
    """

    match = _CVE_RE.search(challenge_name or "")
    if match:
        return f"cve:{match.group(0).upper()}"
    return f"challenge:{challenge_id}"


def assign_split(
    group_key: str,
    *,
    seed: str = "",
    train_pct: int = 80,
    dev_pct: int = 10,
) -> str:
    """Deterministically map a group to train / dev / test.

    Hash-based, not random: re-exporting the same history with the same seed
    must reproduce the same split, or paired A/B comparisons against an older
    export are meaningless.
    """

    digest = hashlib.sha256(f"{seed}|{group_key}".encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) % 100
    if bucket < train_pct:
        return "train"
    if bucket < train_pct + dev_pct:
        return "dev"
    return "test"
