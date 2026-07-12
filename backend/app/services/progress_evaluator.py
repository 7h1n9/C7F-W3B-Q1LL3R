from app.models.challenge import Challenge
from app.models.run import Artifact, Observation, SolveRun
from app.services.skill_router import skill_router
from app.services.solver_state import solver_state_service


class ProgressEvaluator:
    async def evaluate(
        self,
        session,
        run: SolveRun,
        challenge: Challenge,
        tool_name: str,
        result: dict,
        observation: Observation,
        artifact: Artifact,
    ) -> dict:
        confirmed = False
        rejected = False

        facts = dict(observation.facts_json or {})
        facts["tool_name"] = tool_name
        facts["artifact_path"] = artifact.file_path
        facts["artifact_type"] = artifact.artifact_type

        fact_entry = {
            "source": tool_name,
            "challenge_type": challenge.challenge_type,
            "status": result.get("status"),
            "facts": facts,
        }
        if result.get("status") == "COMPLETED" or facts.get("flag_candidate_count"):
            confirmed = await solver_state_service.record_confirmation(
                session, run.id, fact_entry
            )
        else:
            rejected = await solver_state_service.record_rejected_path(
                session,
                run.id,
                {
                    "source": tool_name,
                    "reason": str(result.get("error") or result.get("summary") or "unknown"),
                    "facts": facts,
                },
            )

        recommendations = await skill_router.recommend_from_observation(
            session,
            run.id,
            challenge.challenge_type,
            {
                "summary": observation.summary,
                "facts_json": facts,
                "tool_name": tool_name,
                "observation_id": observation.id,
                "artifact_path": artifact.file_path,
            },
        )
        await solver_state_service.sync_hypotheses(session, run.id)
        made_progress = confirmed or rejected or bool(recommendations)
        no_progress_count = await solver_state_service.record_progress(
            session, run.id, made_progress
        )
        return {
            "confirmed": confirmed,
            "rejected": rejected,
            "recommended_skills": recommendations,
            "made_progress": made_progress,
            "no_progress_count": no_progress_count,
        }


progress_evaluator = ProgressEvaluator()
