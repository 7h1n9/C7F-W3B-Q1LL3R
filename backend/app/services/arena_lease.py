"""Environment lease for the CTF2 arena -- the platform's one-slot critical section.

The constraint this module exists for
-------------------------------------
The arena allows **one running environment per agent**, confirmed from
``whoami`` (``max_environments: 1``) and stated in the platform's own rules
("destroy an environment when you are done with it").  Everything expensive about
arena solving follows from that single number.

Two consequences drive the design:

1. **The lease must span the whole solve, not just the fetch.**  ``submit_flag``
   is refused unless the target is still running -- challenges carry
   ``requires_running_target_for_submit``.  So the order is fixed: start, wait
   for ``access_ready``, hand the URL to the Run, then submit, then destroy.
   Building the local challenge first and starting the Run afterwards would have
   nothing to point the Run at.

2. **A leaked lease blocks everything.**  If a Run crashes and the environment is
   left behind, the single slot stays occupied and every later attempt fails with
   what looks like an unrelated error.  Destruction therefore lives in a
   ``finally``, and :func:`reclaim_orphans` exists for the case where the process
   itself died before that could run.

Readiness is observed, never assumed
------------------------------------
``start`` returns ``status: "starting"`` with ``access_ready: false``; the target
is not reachable until a later ``get_environment`` reports ``access_ready: true``
and supplies ``access_url``.  Measured readiness is a few seconds, but that number
is not relied on -- this polls, with a bounded budget, because a fixed sleep would
be wrong on a slow image and wasteful on a fast one.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from app.core.config import get_settings
from app.core.exceptions import DomainError
from app.core.net import ensure_absolute_http_url

#: How long to wait for ``access_ready`` before giving up.  Container pulls on a
#: cold image are the slow case; observed cold start was ~6s, so this leaves a
#: wide margin without letting a wedged environment hold the slot for an hour.
READY_TIMEOUT_SECONDS = 180
READY_POLL_INTERVAL_SECONDS = 3.0

#: Refresh the environment's lifetime once this fraction of the remaining window
#: has elapsed, so a long solve does not lose its target mid-run.
EXTEND_WHEN_REMAINING_BELOW_SECONDS = 600


def _state_path() -> Path:
    return Path(get_settings().arena_lease_state_file)


def _read_state() -> dict | None:
    """Read the persisted lease breadcrumb.  Missing or unreadable reads as none.

    Unreadable is treated exactly like absent rather than as an error: this file
    only ever enables an *extra* cleanup path, and failing a startup over a
    truncated JSON blob would trade a small convenience for a large outage.
    """
    try:
        payload = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_state(payload: dict | None) -> None:
    """Record (or clear) the environment this process is holding.

    Never raises.  Losing the breadcrumb costs an automatic reclaim on the next
    start and nothing else, so it must not be able to fail the solve that is
    holding a real environment.
    """
    path = _state_path()
    try:
        if payload is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _persist_lease(lease: Lease) -> None:
    """Record enough to destroy this environment without any in-process state."""
    _write_state(
        {
            "connection_id": lease.connection_id,
            "practice_ground_id": lease.practice_ground_id,
            "challenge_id": lease.challenge_id,
            "environment_id": lease.environment_id,
            "access_url": lease.access_url,
            "recorded_at": datetime.now(UTC).isoformat(),
        }
    )



@dataclass
class Lease:
    """A held environment.  ``access_url`` is the target the Run should attack."""

    connection_id: str
    practice_ground_id: str
    challenge_id: str
    environment_id: str = ""
    access_url: str | None = None
    access_type: str | None = None
    #: Every address the environment publishes.  All of them have to be allowed
    #: for outbound traffic or a Run may resolve to one it was not granted.
    allowed_hosts: list[str] = field(default_factory=list)
    expires_at: str | None = None
    remaining_seconds: int | None = None
    started_at: float = field(default_factory=time.monotonic)
    released: bool = False

    def describe(self) -> dict:
        return {
            "environment_id": self.environment_id,
            "access_url": self.access_url,
            "access_type": self.access_type,
            "allowed_hosts": list(self.allowed_hosts),
            "expires_at": self.expires_at,
            "remaining_seconds": self.remaining_seconds,
            "released": self.released,
        }


def hosts_from_access_urls(payload: dict) -> list[str]:
    """Collect the hostnames an environment publishes.

    Ports are deliberately dropped.  ``Challenge.allowed_hosts`` normalises to
    hostnames, and a target arrives on an explicit port such as ``:80``; keeping
    the port in the allowlist entry would fail the hostname membership check that
    ``ChallengeInput`` enforces.

    A bare ``host:port`` is normalised first: ``urlsplit`` reads it as *scheme*
    ``host`` with no hostname, so an unnamed ground would otherwise produce an
    empty allowlist -- a target the solver is allowed to reach but the host
    check cannot match.
    """

    hosts: list[str] = []
    candidates: list[str] = []
    single = payload.get("access_url")
    if isinstance(single, str):
        candidates.append(single)
    many = payload.get("access_urls")
    if isinstance(many, list):
        for entry in many:
            if isinstance(entry, dict) and isinstance(entry.get("url"), str):
                candidates.append(entry["url"])
    for url in candidates:
        hostname = urlsplit(ensure_absolute_http_url(url) or "").hostname
        if hostname and hostname not in hosts:
            hosts.append(hostname)
    return hosts


class ArenaLeaseManager:
    """Serialises environment use across the whole backend.

    Single instance on purpose: the platform's slot is global, so two managers
    would each believe they held the only lease and the second ``start`` would be
    rejected by the platform rather than queued locally.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._current: Lease | None = None
        # Remembered so ``release()`` can destroy the environment even when called
        # without arguments -- which is exactly how ``__aexit__`` calls it.
        self._client: object | None = None

    @property
    def current(self) -> Lease | None:
        return self._current

    async def __aenter__(self) -> "ArenaLeaseManager":
        # Acquired for the whole solve: start, ready-poll, Run, submit, destroy.
        await self._lock.acquire()
        if self._current and not self._current.released:
            self._lock.release()
            raise DomainError(
                "ARENA_LEASE_BUSY",
                "Another arena environment is still held; the platform allows one at a time.",
                status_code=409,
            )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        try:
            await self.release()
        finally:
            self._lock.release()

    async def acquire(
        self, client, *, connection_id: str, practice_ground_id: str, challenge_id: str
    ) -> Lease:
        """Start an environment and wait until it is actually reachable."""
        lease = Lease(
            connection_id=connection_id,
            practice_ground_id=practice_ground_id,
            challenge_id=challenge_id,
        )
        self._client = client
        started = await client.start_environment(
            practice_ground_id=practice_ground_id, challenge_id=challenge_id
        )
        environment = _environment_of(started)
        lease.environment_id = str(environment.get("environment_id") or "")
        lease.expires_at = environment.get("expires_at")
        # Publish it before polling so a failure during readiness still leaves the
        # manager able to destroy it.
        self._current = lease
        _persist_lease(lease)

        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        while True:
            state = _environment_of(
                await client.get_environment(
                    practice_ground_id=practice_ground_id, challenge_id=challenge_id
                )
            )
            lease.remaining_seconds = state.get("remaining_seconds")
            lease.expires_at = state.get("expires_at") or lease.expires_at
            if state.get("access_ready") and state.get("access_url"):
                lease.access_url = str(state["access_url"])
                lease.access_type = state.get("access_type")
                lease.allowed_hosts = hosts_from_access_urls(state)
                return lease
            status = str(state.get("status") or "")
            if status in {"failed", "error", "deleted"}:
                raise DomainError(
                    "ARENA_ENVIRONMENT_FAILED",
                    f"The arena environment entered state '{status}' before becoming ready.",
                    {"environment": state},
                    502,
                    stage="ARENA_ENV",
                )
            if time.monotonic() >= deadline:
                raise DomainError(
                    "ARENA_ENVIRONMENT_TIMEOUT",
                    f"The arena environment did not become ready within {READY_TIMEOUT_SECONDS}s.",
                    {"environment": state},
                    504,
                    stage="ARENA_ENV",
                    retryable=True,
                )
            await asyncio.sleep(READY_POLL_INTERVAL_SECONDS)

    async def keep_alive(self, client, lease: Lease) -> None:
        """Extend the lease when its remaining window gets short.

        Called between Run phases.  A solve that outlives the environment would
        fail at submission with a confusing "target not running" error, so the
        window is refreshed proactively rather than after it expires.
        """
        if lease.released or not lease.environment_id:
            return
        remaining = lease.remaining_seconds
        if remaining is None or remaining > EXTEND_WHEN_REMAINING_BELOW_SECONDS:
            return
        state = _environment_of(
            await client.extend_environment(
                practice_ground_id=lease.practice_ground_id, challenge_id=lease.challenge_id
            )
        )
        lease.remaining_seconds = state.get("remaining_seconds", lease.remaining_seconds)
        lease.expires_at = state.get("expires_at") or lease.expires_at

    async def release(self, client=None) -> None:
        """Destroy the held environment.  Safe to call more than once.

        This is the only thing standing between a crashed solve and a permanently
        unusable agent, so it swallows its own failures after recording them --
        raising here would mask the original error that triggered the cleanup.

        The client falls back to the one ``acquire`` was given.  It has to:
        ``__aexit__`` calls this with no arguments, so an earlier version that
        only destroyed when handed a client never destroyed anything at all --
        the lease was marked released while the environment kept running, and the
        platform's single slot stayed occupied.  A single solve hid it (nothing
        followed to fail); a batch did not, because every later challenge died
        with ``agent_environment_limit``.
        """

        lease = self._current
        if lease is None or lease.released:
            return
        lease.released = True
        client = client or self._client
        if client is None:
            # Genuinely no client: leave the record marked released so orphan
            # reclamation can find the discrepancy on the next connection.  The
            # breadcrumb is left in place for the same reason.
            return
        try:
            await client.delete_environment(
                practice_ground_id=lease.practice_ground_id, challenge_id=lease.challenge_id
            )
        except Exception as error:  # noqa: BLE001 - cleanup must not raise over the real error
            # ``NOT_FOUND`` means the environment is already gone, so there is
            # nothing to reclaim and the breadcrumb would only mislead a later
            # start.  Anything else may still be running -- keep the record, as
            # it is the only thing that lets the next start reclaim it without a
            # human naming the challenge.
            if getattr(error, "code", None) == "NOT_FOUND":
                _write_state(None)
            return
        _write_state(None)

    def force_forget(self) -> None:
        """Drop the in-process record without touching the platform.

        The persisted breadcrumb is deliberately **kept**: this is called exactly
        when the process is giving up on an environment it can no longer manage,
        so the record of it is more valuable afterwards than before.
        """
        self._current = None


def _environment_of(payload: object) -> dict:
    """Unwrap the ``{environment: {...}}`` envelope these tools return."""
    if isinstance(payload, dict):
        environment = payload.get("environment")
        if isinstance(environment, dict):
            return environment
        return payload
    return {}


async def reclaim_persisted_lease(
    client,
    *,
    connection_id: str | None = None,
    practice_ground_id: str | None = None,
    challenge_id: str | None = None,
) -> dict:
    """Destroy the environment recorded by a previous process.  Returns a report.

    This is the *acting* counterpart to :func:`reclaim_orphans`, which can only
    report.  The difference matters: the platform allows one environment per
    agent, so an orphan left by a crash blocks every later solve, and the only
    thing standing between the operator and a wedged agent was knowing which
    challenge to name.  Recording the lease as it is taken removes that guess --
    the process that died wrote down exactly what it had leased.

    An explicit ``practice_ground_id``/``challenge_id`` overrides the record, so
    an operator who knows better (or a record lost with the disk) can still act.

    Destroying an environment that is already gone is a success, not a failure:
    the platform answers a delete for a missing environment with ``isError`` and
    code ``NOT_FOUND``, and treating that as an error would leave the breadcrumb
    behind forever -- which is exactly what happened before this was classified:
    the record from a crash that had already expired on the platform could never
    be cleared, so every later start kept a stale "someone else holds the slot"
    breadcrumb around.  ``NOT_FOUND`` therefore takes the same path as a
    successful delete.
    """

    state = _read_state() or {}
    ground = practice_ground_id or state.get("practice_ground_id")
    challenge = challenge_id or state.get("challenge_id")
    recorded_connection = state.get("connection_id")

    if connection_id and recorded_connection and recorded_connection != connection_id:
        # Another connection's lease.  Destroying it from here would break a
        # solve this connection does not own, and the platform counts the slot
        # per agent -- so a mismatch is information, not an instruction.
        return {
            "reclaimed": False,
            "reason": "lease_belongs_to_another_connection",
            "lease": state,
        }
    if not ground or not challenge:
        return {"reclaimed": False, "reason": "no_lease_record", "lease": state or None}

    already_gone = False
    try:
        await client.delete_environment(
            practice_ground_id=str(ground), challenge_id=str(challenge)
        )
    except Exception as error:  # noqa: BLE001 - reported, not raised: the caller re-probes
        # ``NOT_FOUND`` is the platform's way of saying the environment is
        # already gone -- the desired end state.  Classifying it as a failure
        # would leave the breadcrumb in place forever, so it is treated exactly
        # like a successful delete below.
        if getattr(error, "code", None) != "NOT_FOUND":
            return {
                "reclaimed": False,
                "reason": "delete_failed",
                "error": str(error)[:300],
                "lease": state or {"practice_ground_id": ground, "challenge_id": challenge},
            }
        already_gone = True

    # Cleared once the platform confirms the environment is gone -- either
    # because it accepted the destroy, or because it answered ``NOT_FOUND``
    # (already gone).  A genuine failure leaves the breadcrumb in place for the
    # next attempt.
    _write_state(None)
    return {
        "reclaimed": True,
        "reason": "already_gone" if already_gone else "destroyed",
        "lease": {
            "connection_id": recorded_connection,
            "practice_ground_id": str(ground),
            "challenge_id": str(challenge),
            "environment_id": state.get("environment_id"),
        },
    }


async def reclaim_orphans(client) -> dict:
    """Destroy an environment left behind by a previous process.

    A crash between ``start`` and ``delete`` leaves the single slot occupied with
    no in-process record of it, and every later attempt then fails in a way that
    points at whatever it tried first.  ``whoami`` reports the true count, so the
    agent can be reconciled against platform state rather than trusting its own
    memory of what it started.

    The platform exposes no "list my environments" tool, so the orphan can only be
    removed once its challenge is known.  What this can do unconditionally is
    report the discrepancy, which is what turns a mystery failure into a fact.
    """
    profile = await client.call("ctf2_agent_whoami")
    quotas = profile.get("quotas") if isinstance(profile, dict) else None
    active = quotas.get("active_environments") if isinstance(quotas, dict) else None
    return {
        "active_environments": active,
        "max_environments": (quotas or {}).get("max_environments"),
        "orphan_suspected": bool(active),
        "note": (
            "The platform reports a running environment for this agent. If no solve "
            "is in progress it is an orphan from an interrupted run and must be "
            "destroyed before another environment can start."
        )
        if active
        else "No environment held.",
    }


arena_lease_manager = ArenaLeaseManager()
