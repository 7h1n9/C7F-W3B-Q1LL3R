"""The arena scorebook: record, deduplicate, and aggregate submissions.

Why this module exists
----------------------
The platform does not publish history.  ``submit_flag`` answers one call with one
verdict and retains nothing an agent can read back, so every claim about which
model or engine solved what has to be reconstructed from records written here.

Two behaviours are load-bearing:

* **Deduplication.**  Several models may independently arrive at the same flag for
  the same challenge.  A repeat submission is wasted quota at best, and at worst
  a scoring risk on a platform that penalises wrong or repeated answers.  The
  unique constraint on (connection, challenge, flag) is the guard; this module
  checks before writing so the caller gets a clean answer instead of an integrity
  error.

* **Circuit breaking.**  A challenge that produced only rejections after *n*
  attempts is closed for the attempt run, so a model that keeps proposing
  near-miss flags cannot burn the whole quota on one challenge.  The threshold is
  per (connection, challenge), not per challenge name, because the same name can
  legitimately exist in two arenas.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.arena import ArenaConnection, ArenaSubmission

#: Rejections allowed for one challenge before further submissions are refused.
REJECTION_CIRCUIT_THRESHOLD = 8

VERDICT_ACCEPTED = "accepted"
VERDICT_REJECTED = "rejected"
VERDICT_DUPLICATE = "duplicate"
VERDICT_CIRCUIT_OPEN = "circuit_open"


class SubmissionDecision:
    """Whether a flag may be submitted, and what to record if it may not."""

    def __init__(self, allowed: bool, reason: str, verdict: str) -> None:
        self.allowed = allowed
        self.reason = reason
        self.verdict = verdict


async def evaluate_submission(
    session: AsyncSession, *, connection_id: str, challenge_id: str, flag: str
) -> SubmissionDecision:
    """Decide whether this flag needs to go to the platform at all."""
    existing = await session.scalar(
        select(ArenaSubmission.id).where(
            ArenaSubmission.connection_id == connection_id,
            ArenaSubmission.platform_challenge_id == challenge_id,
            ArenaSubmission.attested_flag == flag,
        )
    )
    if existing:
        return SubmissionDecision(
            False, "This flag was already submitted for this challenge.", VERDICT_DUPLICATE
        )
    rejections = await session.scalar(
        select(func.count(ArenaSubmission.id)).where(
            ArenaSubmission.connection_id == connection_id,
            ArenaSubmission.platform_challenge_id == challenge_id,
            ArenaSubmission.verdict == VERDICT_REJECTED,
        )
    )
    if (rejections or 0) >= REJECTION_CIRCUIT_THRESHOLD:
        return SubmissionDecision(
            False,
            f"{rejections} rejected flags already recorded for this challenge; "
            "further submissions are refused for this attempt run.",
            VERDICT_CIRCUIT_OPEN,
        )
    return SubmissionDecision(True, "", "")


async def record_submission(
    session: AsyncSession,
    *,
    connection_id: str,
    challenge_id: str,
    flag: str,
    verdict: str,
    challenge_name: str = "",
    category: str = "",
    model_config_id: str | None = None,
    engine_key: str | None = None,
    token_cost: int | None = None,
) -> ArenaSubmission:
    """Append one scorebook row.  Idempotent on (connection, challenge, flag)."""
    existing = await session.scalar(
        select(ArenaSubmission).where(
            ArenaSubmission.connection_id == connection_id,
            ArenaSubmission.platform_challenge_id == challenge_id,
            ArenaSubmission.attested_flag == flag,
        )
    )
    if existing:
        return existing
    item = ArenaSubmission(
        connection_id=connection_id,
        platform_challenge_id=challenge_id,
        challenge_name=challenge_name,
        category=category,
        attested_flag=flag,
        verdict=verdict,
        submitted_at=datetime.now(UTC),
        model_config_id=model_config_id,
        engine_key=engine_key,
        token_cost=token_cost,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item


def _rate(accepted: int, attempted: int) -> float | None:
    """Success ratio, or ``None`` when nothing was attempted.

    ``None`` rather than ``0.0``: reporting a rate over an empty denominator would
    read as a total failure instead of an absence of evidence.
    """
    return round(accepted / attempted, 4) if attempted else None


async def aggregate_stats(
    session: AsyncSession, *, connection_id: str | None = None, rows: list[ArenaSubmission] | None = None
) -> dict:
    """Build the ``category x attribution`` matrix.

    The matrix is the point.  A single aggregate solve rate cannot distinguish a
    stronger model from a luckier one, and it hides the structural gaps -- a model
    with no web solves at all looks identical to one that solves only web at a
    lower average.  Both breakdowns are therefore reported alongside the total.
    """
    if rows is None:
        statement = select(ArenaSubmission)
        if connection_id:
            statement = statement.where(ArenaSubmission.connection_id == connection_id)
        rows = list((await session.scalars(statement)).all())

    def bucket(items: list[ArenaSubmission]) -> dict:
        attempted = len(items)
        accepted = sum(1 for item in items if item.verdict == VERDICT_ACCEPTED)
        rejected = sum(1 for item in items if item.verdict == VERDICT_REJECTED)
        return {
            "attempted": attempted,
            "accepted": accepted,
            "rejected": rejected,
            "duplicate_suppressed": sum(
                1 for item in items if item.verdict == VERDICT_DUPLICATE
            ),
            "circuit_open": sum(1 for item in items if item.verdict == VERDICT_CIRCUIT_OPEN),
            "accept_rate": _rate(accepted, attempted),
            "token_cost": sum(item.token_cost or 0 for item in items) or None,
        }

    by_category: dict[str, list[ArenaSubmission]] = {}
    by_model: dict[str, list[ArenaSubmission]] = {}
    by_engine: dict[str, list[ArenaSubmission]] = {}
    for item in rows:
        by_category.setdefault(item.category or "unknown", []).append(item)
        by_model.setdefault(item.model_config_id or "unattributed", []).append(item)
        by_engine.setdefault(item.engine_key or "unattributed", []).append(item)

    # Distinct challenges matter more than raw submission counts: a challenge
    # solved by five models is one solved challenge, not five.
    solved_challenges = {
        item.platform_challenge_id for item in rows if item.verdict == VERDICT_ACCEPTED
    }
    touched_challenges = {item.platform_challenge_id for item in rows}

    return {
        "total": bucket(rows),
        "distinct_challenges_touched": len(touched_challenges),
        "distinct_challenges_solved": len(solved_challenges),
        "by_category": {key: bucket(value) for key, value in sorted(by_category.items())},
        "by_model": {key: bucket(value) for key, value in sorted(by_model.items())},
        "by_engine": {key: bucket(value) for key, value in sorted(by_engine.items())},
    }


async def connection_rows(session: AsyncSession) -> list[ArenaConnection]:
    items = list((await session.scalars(select(ArenaConnection))).all())
    items.sort(key=lambda item: item.created_at)
    return items
