from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.challenge import Challenge
from app.models.run import Artifact, Hypothesis, Observation, SolveRun, ToolCall
from app.tools.registry import load_tool_definitions


class ContextBuilder:
    """Build a bounded, artifact-first model context for one agent step."""

    async def build(self, session: AsyncSession, run: SolveRun, challenge: Challenge) -> list[dict]:
        limit = run.max_context_observations
        observations = list((await session.scalars(
            select(Observation).where(Observation.run_id == run.id).order_by(Observation.created_at.desc()).limit(limit)
        )).all())
        artifacts = list((await session.scalars(
            select(Artifact).where(Artifact.run_id == run.id).order_by(Artifact.created_at.desc()).limit(limit)
        )).all())
        hypotheses = list((await session.scalars(
            select(Hypothesis).where(Hypothesis.run_id == run.id).order_by(Hypothesis.updated_at.desc()).limit(limit)
        )).all())
        calls = list((await session.scalars(
            select(ToolCall).where(ToolCall.run_id == run.id).order_by(ToolCall.created_at.desc()).limit(limit)
        )).all())
        tools = load_tool_definitions()
        tool_schema = [{"name": item.name, "description": item.description, "parameters": item.parameters} for item in tools.values() if item.enabled]
        facts = {
            "challenge": {"name": challenge.name, "description": challenge.description, "target_url": challenge.target_url, "allowed_hosts": challenge.allowed_hosts, "flag_pattern": challenge.flag_pattern},
            "run": {"status": run.status, "remaining_steps": max(run.max_agent_steps - run.agent_step_count, 0), "remaining_tool_calls": max(run.max_tool_calls - run.tool_call_count, 0)},
            "available_tools": tool_schema,
            "hypotheses": [{"id": item.id, "title": item.title, "status": item.status, "confidence": item.confidence} for item in hypotheses],
            "recent_observations": [{"summary": item.summary, "facts": item.facts_json} for item in reversed(observations)],
            "recent_tool_calls": [{"tool": item.tool_name, "status": item.status} for item in reversed(calls)],
            "artifacts": [{"path": item.file_path, "type": item.artifact_type, "summary": item.summary} for item in reversed(artifacts)],
        }
        system = (
            "You solve only this authorized CTF Web challenge. Never request shell commands, scanners, payload libraries, "
            "or targets outside allowed_hosts. Use only the listed tools. Return exactly one JSON action. Context contains "
            "summaries only: use file_read on an artifact path when raw content is needed."
        )
        return [{"role": "system", "content": system}, {"role": "user", "content": str(facts)}]


context_builder = ContextBuilder()
