from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.skill import Skill
from app.models.solver_state import SolverState


class SkillRouter:
    async def activate_from_observation(
        self, session: AsyncSession, run_id: str, observation: dict
    ) -> list[str]:
        state = await session.scalar(select(SolverState).where(SolverState.run_id == run_id))
        if not state:
            return []
        active_ids = set(state.active_skill_ids_json or [])
        observation_blob = " ".join(
            [
                str(observation.get("summary") or ""),
                str(observation.get("facts_json") or {}),
                str(observation.get("tool_name") or ""),
            ]
        ).lower()
        candidates = list(
            (
                await session.scalars(
                    select(Skill).where(
                        Skill.enabled,
                        Skill.activation_mode == "AUTO",
                        Skill.skill_kind == "SPECIALIST",
                    )
                )
            ).all()
        )
        activated: list[str] = []
        for skill in candidates:
            if skill.id in active_ids:
                continue
            triggers = [item.lower() for item in (skill.triggers or [])]
            if not triggers:
                continue
            if any(trigger in observation_blob for trigger in triggers):
                active_ids.add(skill.id)
                activated.append(skill.id)
        if activated:
            state.active_skill_ids_json = sorted(active_ids)
            await session.commit()
        return activated


skill_router = SkillRouter()
