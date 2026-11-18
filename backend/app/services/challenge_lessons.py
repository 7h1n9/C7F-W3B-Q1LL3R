"""Safe strategy-only lessons extracted from prior successful runs.

Two views over the same statistics:

* ``extract`` / ``for_context`` — the original same-challenge view;
* ``extract_by_profile`` — the cross-challenge view (CAP-11), which pools the
  solved Runs of *other* challenges that share this one's vulnerability type and
  (optionally) target stack.

Both views run through ``_aggregate``, so a cross-challenge number and a
same-challenge number cannot drift apart.  Which challenges count as "the same
kind" is decided from ``Challenge.metadata_json`` only: it is written by the
benchmark entry point from the case definition, so it is checkable, and a
challenge with no ``vulnerability_type`` simply cannot join a profile instead of
being guessed into one.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.challenge import Challenge
from app.models.run import RunEvent, SolveRun, ToolCall
from app.services.action_fingerprint import argument_shape

LESSON_KEYS = (
    "effective_tool_order",
    "verified_attack_surface",
    "common_blockers",
    "argument_shapes",
    "avoid_repeated_behaviors",
)

# The profile view is self-produced knowledge, so it gets a small budget: it
# must be readable next to the human-reviewed corpus, not instead of it.
MAX_PROFILE_PRIOR_CHARS = 600
MAX_PROFILE_LESSONS = 4
DEFAULT_RUN_LIMIT = 20


def empty_lessons() -> dict[str, list[str]]:
    return {key: [] for key in LESSON_KEYS}


@dataclass(frozen=True, slots=True)
class ProfileLessons:
    """Cross-challenge lessons plus what they were aggregated over."""

    lessons: dict[str, list[str]] = field(default_factory=empty_lessons)
    vulnerability_type: str = ""
    target_stack: tuple[str, ...] = ()
    challenge_count: int = 0
    run_count: int = 0

    @property
    def usable(self) -> bool:
        return self.run_count > 0 and any(self.lessons.get(key) for key in LESSON_KEYS)


class ChallengeLessonService:
    modes = {"disabled", "strategy_only", "full_replay_for_debug"}

    def __init__(self, mode: str | None = None) -> None:
        if mode is None:
            try:
                from app.core.config import get_settings
                mode = get_settings().historical_lesson_mode
            except Exception:
                mode = "strategy_only"
        self.mode = mode if mode in self.modes else "strategy_only"

    async def extract(self, session: AsyncSession, challenge_id: str, *, exclude_run_id: str | None = None) -> dict:
        if self.mode == "disabled":
            return empty_lessons()
        runs = await self._solved_runs(
            session,
            challenge_ids=[challenge_id],
            exclude_run_id=exclude_run_id,
        )
        return await self._aggregate(session, runs)

    async def for_context(self, session: AsyncSession, challenge_id: str, *, exclude_run_id: str | None = None) -> dict:
        return await self.extract(session, challenge_id, exclude_run_id=exclude_run_id)

    async def extract_by_profile(
        self,
        session: AsyncSession,
        *,
        vulnerability_type: str,
        target_stack: Sequence[str] = (),
        exclude_challenge_id: str | None = None,
        limit: int = DEFAULT_RUN_LIMIT,
    ) -> ProfileLessons:
        """Pool solved Runs across other challenges of the same kind (CAP-11).

        ``exclude_challenge_id`` keeps the current challenge out, so a new
        challenge with no history of its own still gets a prior instead of an
        empty one.
        """

        wanted_type = str(vulnerability_type or "").strip().upper()
        wanted_stack = {
            str(item).strip().casefold() for item in (target_stack or []) if str(item).strip()
        }
        if self.mode == "disabled" or not wanted_type:
            return ProfileLessons(vulnerability_type=wanted_type)

        candidates = list((await session.scalars(select(Challenge))).all())
        matched: list[str] = []
        for challenge in candidates:
            if str(challenge.id) == str(exclude_challenge_id or ""):
                continue
            metadata = challenge.metadata_json if isinstance(challenge.metadata_json, dict) else {}
            if str(metadata.get("vulnerability_type") or "").strip().upper() != wanted_type:
                continue
            if wanted_stack:
                stack = {
                    str(item).strip().casefold()
                    for item in (metadata.get("target_stack") or [])
                    if str(item).strip()
                }
                if not stack & wanted_stack:
                    continue
            matched.append(str(challenge.id))

        if not matched:
            return ProfileLessons(vulnerability_type=wanted_type, target_stack=tuple(sorted(wanted_stack)))

        runs = await self._solved_runs(session, challenge_ids=matched, exclude_run_id=None, limit=limit)
        return ProfileLessons(
            lessons=await self._aggregate(session, runs),
            vulnerability_type=wanted_type,
            target_stack=tuple(sorted(wanted_stack)),
            challenge_count=len(matched),
            run_count=len(runs),
        )

    async def _solved_runs(
        self,
        session: AsyncSession,
        *,
        challenge_ids: Sequence[str],
        exclude_run_id: str | None,
        limit: int = DEFAULT_RUN_LIMIT,
    ) -> list[SolveRun]:
        if not challenge_ids:
            return []
        statement = (
            select(SolveRun)
            .where(
                SolveRun.challenge_id.in_(list(challenge_ids)),
                SolveRun.status == "COMPLETED_SOLVED",
                SolveRun.id != (exclude_run_id or ""),
            )
            # Newest first: lessons should describe the current behaviour of the
            # solver, and a hard limit keeps a long-lived challenge from making
            # every context build scan its whole history.
            .order_by(SolveRun.created_at.desc())
            .limit(max(int(limit), 1))
        )
        return list((await session.scalars(statement)).all())

    async def _aggregate(self, session: AsyncSession, runs: Sequence[SolveRun]) -> dict[str, list[str]]:
        """The single place both lesson views derive their numbers from."""

        order: Counter[str] = Counter()
        surfaces: Counter[str] = Counter()
        blockers: Counter[str] = Counter()
        shapes: Counter[str] = Counter()
        repeats: Counter[str] = Counter()
        for run in runs:
            calls = list((await session.scalars(select(ToolCall).where(ToolCall.run_id == run.id).order_by(ToolCall.created_at))).all())
            seen: set[str] = set()
            for call in calls:
                tool = str(call.tool_name).removeprefix("ctfctl.")
                if tool not in seen:
                    order[tool] += 1
                    seen.add(tool)
                # Same shape string as app.services.action_fingerprint so the
                # lessons and the process metrics speak one vocabulary.
                shapes[argument_shape(call.tool_name, call.arguments_json)] += 1
            events = list((await session.scalars(select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.sequence))).all())
            for event in events:
                payload = event.payload_json or {}
                code = str(payload.get("code") or payload.get("error_code") or "")
                if code and code not in {"CODEX_DIRECT_TOOL_FORBIDDEN"}:
                    blockers[code] += 1
                tool = str(payload.get("tool") or "").removeprefix("ctfctl.")
                if tool and event.event_type == "tool.completed":
                    surfaces[tool] += 1
                if event.event_type == "agent.action_rejected" and code:
                    repeats[code] += 1
        return {
            "effective_tool_order": [name for name, _ in order.most_common(8)],
            "verified_attack_surface": [name for name, _ in surfaces.most_common(8)],
            "common_blockers": [name for name, _ in blockers.most_common(8)],
            "argument_shapes": [name for name, _ in shapes.most_common(8)],
            "avoid_repeated_behaviors": [name for name, _ in repeats.most_common(8)],
        }


def profile_of(challenge) -> tuple[str, tuple[str, ...]]:
    """Read a challenge's ``(vulnerability_type, target_stack)`` from metadata."""

    metadata = getattr(challenge, "metadata_json", None)
    if not isinstance(metadata, dict):
        return "", ()
    vulnerability_type = str(metadata.get("vulnerability_type") or "").strip().upper()
    stack = tuple(
        str(item).strip() for item in (metadata.get("target_stack") or []) if str(item).strip()
    )
    return vulnerability_type, stack


def format_profile_lessons(profile: ProfileLessons, *, max_chars: int = MAX_PROFILE_PRIOR_CHARS) -> str:
    """Render cross-challenge lessons as a bounded, explicitly self-produced block.

    Returns ``""`` when there is nothing worth saying, so a caller can append the
    result unconditionally.
    """

    if not profile.usable or max_chars <= 0:
        return ""

    def _line(label: str, key: str) -> str:
        values = [str(item) for item in profile.lessons.get(key, []) if str(item)][:MAX_PROFILE_LESSONS]
        return f"{label}: {', '.join(values)}" if values else ""

    header = (
        f"Prior from THIS project's own solved {profile.vulnerability_type} challenges "
        f"({profile.challenge_count} challenge(s), {profile.run_count} run(s); "
        "自产先验，可信级别低于人工整理语料，仅作路线参考):"
    )
    body = [
        _line("Openers that led to a solve", "effective_tool_order"),
        _line("Endpoints that produced evidence", "verified_attack_surface"),
        _line("Blockers seen before", "common_blockers"),
        _line("Argument shapes that recurred", "argument_shapes"),
    ]
    rendered = "\n".join([header, *[line for line in body if line]])
    if len(rendered) <= max_chars:
        return rendered
    suffix = "\n[profile prior truncated]"
    return rendered[: max(max_chars - len(suffix), 0)].rstrip() + suffix


challenge_lesson_service = ChallengeLessonService()
