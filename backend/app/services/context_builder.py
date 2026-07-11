import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.challenge import Challenge
from app.models.run import Artifact, Hypothesis, Observation, SolveRun, ToolCall
from app.models.skill import RunSkillSnapshot
from app.services.skill_selection import allowed_tools_for
from app.tools.registry import load_tool_definitions


class ContextBuilder:
    """Build a bounded, artifact-first model context for one agent step."""

    async def build(self, session: AsyncSession, run: SolveRun, challenge: Challenge) -> list[dict]:
        limit = run.max_context_observations
        observations = list(
            (
                await session.scalars(
                    select(Observation)
                    .where(Observation.run_id == run.id)
                    .order_by(Observation.created_at.desc())
                    .limit(limit)
                )
            ).all()
        )
        artifacts = list(
            (
                await session.scalars(
                    select(Artifact)
                    .where(Artifact.run_id == run.id)
                    .order_by(Artifact.created_at.desc())
                    .limit(limit)
                )
            ).all()
        )
        hypotheses = list(
            (
                await session.scalars(
                    select(Hypothesis)
                    .where(Hypothesis.run_id == run.id)
                    .order_by(Hypothesis.updated_at.desc())
                    .limit(limit)
                )
            ).all()
        )
        calls = list(
            (
                await session.scalars(
                    select(ToolCall)
                    .where(ToolCall.run_id == run.id)
                    .order_by(ToolCall.created_at.desc())
                    .limit(limit)
                )
            ).all()
        )
        tools = load_tool_definitions()
        snapshots = list(
            (
                await session.scalars(
                    select(RunSkillSnapshot)
                    .where(RunSkillSnapshot.run_id == run.id)
                    .order_by(RunSkillSnapshot.priority)
                )
            ).all()
        )
        permitted = allowed_tools_for(challenge.challenge_type)
        for snapshot in snapshots:
            if snapshot.allowed_tools_snapshot:
                permitted &= set(snapshot.allowed_tools_snapshot)
        tool_schema = [
            {"name": item.name, "description": item.description, "parameters": item.parameters}
            for item in tools.values()
            if item.enabled and item.name in permitted
        ]
        facts = {
            "challenge": {
                "name": challenge.name,
                "description": challenge.description,
                "challenge_type": challenge.challenge_type,
                "target_url": challenge.target_url,
                "allowed_hosts": challenge.allowed_hosts,
                "flag_pattern": challenge.flag_pattern,
            },
            "run": {
                "status": run.status,
                "remaining_steps": max(run.max_agent_steps - run.agent_step_count, 0),
                "remaining_tool_calls": max(run.max_tool_calls - run.tool_call_count, 0),
            },
            "available_tools": tool_schema,
            "hypotheses": [
                {
                    "id": item.id,
                    "title": item.title,
                    "status": item.status,
                    "confidence": item.confidence,
                }
                for item in hypotheses
            ],
            "recent_observations": [
                {"summary": item.summary, "facts": item.facts_json}
                for item in reversed(observations)
            ],
            "recent_tool_calls": [
                {"tool": item.tool_name, "status": item.status} for item in reversed(calls)
            ],
            "artifacts": [
                {"path": item.file_path, "type": item.artifact_type, "summary": item.summary}
                for item in reversed(artifacts)
            ],
            "skills": [
                {
                    "name": item.skill_name,
                    "version": item.skill_version,
                    "allowed_tools": item.allowed_tools_snapshot,
                    "content": item.content_snapshot,
                }
                for item in snapshots
            ],
            "conversation_summary": run.conversation_summary,
        }
        system = (
            "You solve only this authorized CTF Web challenge. Never request shell commands, scanners, payload libraries, "
            "or targets outside allowed_hosts. Use only the listed tools. Return exactly one JSON action. Context contains "
            "summaries only: use file_read on an artifact path when raw content is needed."
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(facts, ensure_ascii=False)},
        ]


context_builder = ContextBuilder()
