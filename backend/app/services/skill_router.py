from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.skill import Skill
from app.services.solver_state import solver_state_service


class SkillRouter:
    async def recommend_from_observation(
        self,
        session: AsyncSession,
        run_id: str,
        challenge_type: str,
        observation: dict,
    ) -> list[dict]:
        state = await solver_state_service.load(session, run_id)
        if not state:
            return []
        active_ids = set(state.active_skill_ids_json or [])
        observation_blob = " ".join(
            [
                str(observation.get("summary") or ""),
                str(observation.get("facts_json") or {}),
                str(observation.get("tool_name") or ""),
                str(observation.get("artifact_path") or ""),
            ]
        ).lower()
        candidates = list(
            (
                await session.scalars(
                    select(Skill).where(
                        Skill.enabled,
                        Skill.skill_kind == "SPECIALIST",
                    )
                )
            ).all()
        )
        recommendations: list[dict] = []
        for skill in candidates:
            if challenge_type not in (skill.challenge_types or []):
                continue
            if skill.id in active_ids:
                continue
            triggers = [item.lower() for item in (skill.triggers or []) if item]
            matched = [trigger for trigger in triggers if trigger in observation_blob]
            if not matched:
                continue
            confidence = min(95, 40 + len(matched) * 15 + min(len(observation_blob) // 120, 20))
            recommendations.append(
                {
                    "skill_id": skill.id,
                    "skill_name": skill.name,
                    "display_name": skill.display_name,
                    "matched_triggers": matched,
                    "confidence": confidence,
                    "reason": f"Observation matched {', '.join(matched)}.",
                    "supporting_observation_ids": [observation.get("observation_id")]
                    if observation.get("observation_id")
                    else [],
                }
            )
        recommendations.sort(key=lambda item: (-item["confidence"], item["skill_name"]))
        await solver_state_service.record_skill_recommendations(session, run_id, recommendations)
        return recommendations


skill_router = SkillRouter()
