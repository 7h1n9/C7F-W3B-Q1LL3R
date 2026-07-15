"""Safe strategy-only lessons extracted from prior successful runs."""
from __future__ import annotations

from collections import Counter

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.run import RunEvent, SolveRun, ToolCall


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
            return {"effective_tool_order": [], "verified_attack_surface": [], "common_blockers": [], "argument_shapes": [], "avoid_repeated_behaviors": []}
        runs = list((await session.scalars(select(SolveRun).where(SolveRun.challenge_id == challenge_id, SolveRun.status == "COMPLETED_SOLVED", SolveRun.id != (exclude_run_id or "")))).all())
        order: Counter[str] = Counter(); surfaces: Counter[str] = Counter(); blockers: Counter[str] = Counter(); shapes: Counter[str] = Counter(); repeats: Counter[str] = Counter()
        for run in runs[:20]:
            calls = list((await session.scalars(select(ToolCall).where(ToolCall.run_id == run.id).order_by(ToolCall.created_at))).all())
            seen: set[str] = set()
            for call in calls:
                tool = str(call.tool_name).removeprefix("ctfctl.")
                if tool not in seen:
                    order[tool] += 1; seen.add(tool)
                shapes[tool + "{" + ",".join(sorted((call.arguments_json or {}).keys())) + "}"] += 1
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

    async def for_context(self, session: AsyncSession, challenge_id: str, *, exclude_run_id: str | None = None) -> dict:
        return await self.extract(session, challenge_id, exclude_run_id=exclude_run_id)


challenge_lesson_service = ChallengeLessonService()
