"""Cross-worker Insight Bus, ported from the official Muteki swarm.

When N workers race the same challenge, they share VERIFIED OBJECTIVE FACTS
only -- never guesses or plans (sharing speculation would bias the whole swarm
toward one worker's wrong idea).  Message kinds:

    FACT        -- a confirmed fact (leaked cred, service version/CVE, etc.)
                    Other workers fold it into their evidence.
    DEAD_END    -- a refuted path. Other workers MUST prune it.
    FLAG        -- the first valid flag. The swarm cancels the rest.
    ALL_FLAGS_FOUND -- the run collected every expected flag; siblings STOP.
    GUIDANCE    -- HITL: a human command/hint injected into the run.
    SUBMIT_LOCKED / SUBMIT_UNLOCKED / VERIFIER_LOCKED
                -- rate-limited verifier coordination for chained-submit CTFs.

This is a per-challenge fan-out hub, separate from the graph event log (which
is for the frontend / audit).  A worker publishes here; the bus pushes to every
OTHER worker's inbox.  The publishing worker does not receive its own messages
back.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Optional

_HISTORY_CAP = 1000  # bound the backlog so long runs do not grow memory unbounded


class InsightKind(StrEnum):
    FACT = "FactDiscovered"
    DEAD_END = "DeadEndMarked"
    FLAG = "FlagFound"
    ALL_FLAGS_FOUND = "AllFlagsFound"
    GUIDANCE = "HumanGuidance"
    SUBMIT_LOCKED = "SubmitLocked"
    SUBMIT_UNLOCKED = "SubmitUnlocked"
    VERIFIER_LOCKED = "VerifierLocked"


@dataclass
class Insight:
    kind: InsightKind
    by: str
    text: str
    artifact_id: Optional[str] = None
    action: str = ""
    target: str = ""
    url: str = ""
    standing: bool = False


@dataclass
class InsightBus:
    """Per-challenge fan-out of verified insights to all participating workers.

    Each worker registers an asyncio.Queue inbox.  publish() pushes to every
    inbox EXCEPT the producer's own.  A late subscriber still gets the backlog
    of what was already broadcast, which is why `history` is kept.
    """

    challenge_id: str
    _inboxes: dict[str, asyncio.Queue[Insight]] = field(default_factory=dict)
    history: deque[Insight] = field(default_factory=lambda: deque(maxlen=_HISTORY_CAP))
    _flags: list[str] = field(default_factory=list)

    def subscribe(self, solver_id: str) -> asyncio.Queue[Insight]:
        q: asyncio.Queue[Insight] = asyncio.Queue()
        self._inboxes[solver_id] = q
        for ins in self.history:
            if ins.by != solver_id:
                q.put_nowait(ins)
        return q

    def unsubscribe(self, solver_id: str) -> None:
        self._inboxes.pop(solver_id, None)

    async def publish(self, ins: Insight) -> None:
        if ins.kind is InsightKind.GUIDANCE and self._is_duplicate_guidance(ins):
            return
        self.history.append(ins)
        if ins.kind is InsightKind.FLAG and ins.text and ins.text not in self._flags:
            self._flags.append(ins.text)
        for sid, q in self._inboxes.items():
            if sid != ins.by:
                await q.put(ins)

    def _is_duplicate_guidance(self, ins: Insight) -> bool:
        for prev in reversed(self.history):
            if prev.kind is not InsightKind.GUIDANCE:
                return False
            if (prev.text == ins.text and prev.action == ins.action
                    and prev.target == ins.target and prev.url == ins.url
                    and prev.standing == ins.standing):
                return True
        return False

    # convenience producers ------------------------------------------------
    async def fact(self, by: str, text: str, artifact_id: Optional[str] = None) -> None:
        await self.publish(Insight(InsightKind.FACT, by, text, artifact_id))

    async def dead_end(self, by: str, reason: str) -> None:
        await self.publish(Insight(InsightKind.DEAD_END, by, reason))

    async def flag_found(self, by: str, flag: str) -> None:
        await self.publish(Insight(InsightKind.FLAG, by, flag))

    async def all_flags_found(self, by: str, *, count: int = 0) -> None:
        await self.publish(Insight(InsightKind.ALL_FLAGS_FOUND, by, str(count)))

    async def submit_locked(self, by: str) -> None:
        await self.publish(Insight(InsightKind.SUBMIT_LOCKED, by, ""))

    async def submit_unlocked(self, by: str, result: str = "") -> None:
        await self.publish(Insight(InsightKind.SUBMIT_UNLOCKED, by, result))

    async def verifier_locked(self, by: str, seconds_remaining: int) -> None:
        await self.publish(Insight(InsightKind.VERIFIER_LOCKED, by, str(seconds_remaining)))

    async def guidance(self, by: str, text: str, *, action: str = "",
                       target: str = "", url: str = "", standing: bool = False) -> None:
        await self.publish(Insight(InsightKind.GUIDANCE, by, text,
                                   action=action, target=target, url=url,
                                   standing=standing))
