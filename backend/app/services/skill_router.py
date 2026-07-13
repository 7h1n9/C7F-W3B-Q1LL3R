from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.skill import Skill
from app.services.solver_state import solver_state_service


class SkillRouter:
    NOISY_TRIGGERS = {
        "url",
        "path",
        "file",
        "user",
        "parameter",
        "cookie",
        "error",
        "comment",
        "token",
    }

    @staticmethod
    def _fact_values(facts: dict) -> list[str]:
        values: list[str] = []
        for value in facts.values():
            if isinstance(value, dict):
                values.extend(SkillRouter._fact_values(value))
            elif isinstance(value, list):
                values.extend(str(item).lower() for item in value[:50])
            else:
                values.append(str(value).lower())
        return values

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
        facts = observation.get("extracted_facts") or observation.get("facts_json") or {}
        fact_values = self._fact_values(facts if isinstance(facts, dict) else {})
        evidence_fingerprint = str(observation.get("evidence_fingerprint") or facts.get("evidence_fingerprint") or "")
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
            matched = sorted(
                {
                    trigger
                    for trigger in triggers
                    if any(trigger in value for value in fact_values)
                }
            )
            if not matched:
                continue
            strong_matches = [trigger for trigger in matched if trigger not in self.NOISY_TRIGGERS]
            if not strong_matches:
                continue
            negative = [
                trigger
                for trigger in (getattr(skill, "negative_triggers", []) or [])
                if any(str(trigger).lower() in value for value in fact_values)
            ]
            confidence = min(95, 45 + len(strong_matches) * 15 + len(matched) * 5 - len(negative) * 20)
            if confidence < 60:
                continue
            recommendation_id = f"{run_id}:{skill.id}:{evidence_fingerprint or '-'}"
            existing = state.skill_recommendations_json or []
            if any(item.get("recommendation_id") == recommendation_id for item in existing):
                continue
            recommendations.append(
                {
                    "recommendation_id": recommendation_id,
                    "skill_id": skill.id,
                    "skill_name": skill.name,
                    "display_name": skill.display_name,
                    "matched_positive_triggers": matched,
                    "matched_negative_triggers": negative,
                    "confidence": confidence,
                    "reason": f"Observation matched {', '.join(matched)}.",
                    "supporting_fact_ids": ([str(item) for item in facts.get("fact_ids", [])] if isinstance(facts, dict) else []) or ([str(observation.get("observation_id"))] if observation.get("observation_id") else []),
                    "supporting_observation_ids": [observation.get("observation_id")]
                    if observation.get("observation_id")
                    else [],
                }
            )
        recommendations.sort(key=lambda item: (-item["confidence"], item["skill_name"]))
        merged = [*recommendations, *(state.skill_recommendations_json or [])]
        best: dict[str, dict] = {}
        for item in merged:
            key = str(item.get("skill_id") or item.get("skill_name"))
            if key and item.get("confidence", 0) > best.get(key, {}).get("confidence", -1):
                best[key] = item
        await solver_state_service.record_skill_recommendations(
            session,
            run_id,
            sorted(best.values(), key=lambda item: (-item.get("confidence", 0), item.get("skill_name", "")))[:5],
        )
        return recommendations


skill_router = SkillRouter()
