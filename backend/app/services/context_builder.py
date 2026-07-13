import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.challenge import Challenge
from app.models.run import Artifact, FlagCandidate, Hypothesis, Observation, SolveRun, ToolCall
from app.models.skill import RunSkillSnapshot, Skill
from app.services.run_diagnostics import run_diagnostics_service
from app.services.skill_selection import specialist_skill_catalog
from app.services.solver_state import solver_state_service
from app.services.tool_permissions import effective_tools_for
from app.tools.registry import load_tool_definitions

CORE_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "ctf_solver_core.md"
CORE_PROMPT = CORE_PROMPT_PATH.read_text(encoding="utf-8")


class ContextBuilder:
    """Build the bounded, phase-ordered context for one agent step."""

    @staticmethod
    def _ordered_hypotheses(items: list[Hypothesis]) -> list[dict]:
        ordered = sorted(items, key=lambda item: (-item.priority, -item.confidence, item.created_at))
        return [
            {
                "id": item.id,
                "category": item.category,
                "statement": item.title,
                "description": item.description,
                "confidence": item.confidence,
                "priority": item.priority,
                "status": item.status,
                "evidence": item.evidence_json,
                "attempt_count": item.attempt_count,
            }
            for item in ordered
        ]

    @staticmethod
    def _tool_schema(tools: dict[str, object], permitted: set[str]) -> list[dict]:
        return [
            {"name": item.name, "description": item.description, "parameters": item.parameters}
            for item in tools.values()
            if item.enabled and item.name in permitted
        ]

    async def build(self, session: AsyncSession, run: SolveRun, challenge: Challenge) -> list[dict]:
        state = await solver_state_service.load(session, run.id)
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
        # Keep one observation for the same semantic result. This is the first
        # trim stage because repeated polling otherwise dominates provider input.
        unique_observations: list[Observation] = []
        seen_observations: set[str] = set()
        for item in observations:
            signature = hashlib.sha256(json.dumps([item.summary, item.facts_json], ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
            if signature not in seen_observations:
                unique_observations.append(item); seen_observations.add(signature)
        observations = unique_observations
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
        snapshots = list(
            (
                await session.scalars(
                    select(RunSkillSnapshot)
                    .where(RunSkillSnapshot.run_id == run.id)
                    .order_by(RunSkillSnapshot.priority)
                )
            ).all()
        )
        skill_ids = {snapshot.skill_id for snapshot in snapshots}
        skills = (
            {
                item.id: item
                for item in (
                    await session.scalars(select(Skill).where(Skill.id.in_(skill_ids)))
                ).all()
            }
            if skill_ids
            else {}
        )
        permitted = await effective_tools_for(session, run, challenge)
        tool_schema = self._tool_schema(load_tool_definitions(), permitted)
        active_skill_ids = set((state.active_skill_ids_json if state else []) or [])
        raw_recommendations = (state.skill_recommendations_json if state else []) or []
        best_recommendations: dict[str, dict] = {}
        for recommendation in raw_recommendations:
            name = str(recommendation.get("skill_name") or recommendation.get("skill_id") or "")
            if name and recommendation.get("confidence", 0) >= best_recommendations.get(name, {}).get("confidence", -1):
                best_recommendations[name] = recommendation
        skill_recommendations = list(best_recommendations.values())
        active_snapshots = [item for item in snapshots if item.skill_id in active_skill_ids]
        specialist_catalog = await specialist_skill_catalog(session, challenge.challenge_type, active_skill_ids)
        snapshot_by_skill = {snapshot.skill_id: snapshot for snapshot in snapshots}
        active_skill_details = [
            {
                "skill_id": snapshot.skill_id,
                "name": snapshot.skill_name,
                "version": snapshot.skill_version,
                "skill_kind": skills.get(snapshot.skill_id).skill_kind if skills.get(snapshot.skill_id) else "SPECIALIST",
                "activation_mode": skills.get(snapshot.skill_id).activation_mode if skills.get(snapshot.skill_id) else "MANUAL",
                "triggers": skills.get(snapshot.skill_id).triggers if skills.get(snapshot.skill_id) else [],
                "required_tools": skills.get(snapshot.skill_id).required_tools if skills.get(snapshot.skill_id) else [],
                "recommended_tools": skills.get(snapshot.skill_id).recommended_tools if skills.get(snapshot.skill_id) else [],
                "forbidden_tools": skills.get(snapshot.skill_id).forbidden_tools if skills.get(snapshot.skill_id) else [],
                "ctf_phases": skills.get(snapshot.skill_id).ctf_phases if skills.get(snapshot.skill_id) else [],
                "content": snapshot.content_snapshot,
                "config": snapshot.config_snapshot,
            }
            for snapshot in active_snapshots
            if skills.get(snapshot.skill_id)
        ]
        candidate_skill_summaries = [
            {
                "skill_id": skill.id,
                "name": skill.name,
                "display_name": skill.display_name,
                "version": skill.version,
                "skill_kind": skill.skill_kind,
                "activation_mode": skill.activation_mode,
                "triggers": skill.triggers,
                "prerequisites": skill.prerequisites,
                "required_tools": skill.required_tools,
                "recommended_tools": skill.recommended_tools,
                "forbidden_tools": skill.forbidden_tools,
                "ctf_phases": skill.ctf_phases,
                "snapshotted": skill.id in snapshot_by_skill,
                "priority": snapshot_by_skill.get(skill.id).priority if skill.id in snapshot_by_skill else None,
            }
            for skill in specialist_catalog
        ]
        last_call = calls[0] if calls else None
        last_observation = observations[0] if observations else None
        recent_runs = list(
            (
                await session.scalars(
                    select(SolveRun)
                    .where(SolveRun.challenge_id == challenge.id, SolveRun.id != run.id)
                    .order_by(SolveRun.created_at.desc())
                    .limit(5)
                )
            ).all()
        )
        lessons = {"known_failure_modes": [], "verified_paths": [], "avoid_repeated_behaviors": []}
        for recent_run in recent_runs:
            diag = await run_diagnostics_service.analyze(session, recent_run)
            lessons["known_failure_modes"].extend(diag["diagnostic_tags"])
            if recent_run.status == "COMPLETED_SOLVED":
                lessons["verified_paths"].append(f"{recent_run.engine_type} completed a verified run")
            lessons["avoid_repeated_behaviors"].extend(item["code"] for item in diag["anomalies"])
        for key in lessons:
            lessons[key] = sorted(set(lessons[key]))[:12]
        latest_tool_view = (last_observation.facts_json or {}).get("tool_model_view") if last_observation else None
        started_at = run.started_at
        if started_at and started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)
        context = {
            "Role Snapshot": run.role_snapshot_json or {},
            "Challenge-Type Methodology Skill": {
                "challenge_type": challenge.challenge_type,
                "current_phase": (state.current_phase if state else run.current_phase),
                "skill_names": [snapshot.skill_name for snapshot in snapshots],
                "methodology_skills": [
                    {
                        "skill_id": snapshot.skill_id,
                        "name": snapshot.skill_name,
                        "version": snapshot.skill_version,
                        "content": snapshot.content_snapshot,
                    }
                    for snapshot in snapshots
                    if skills.get(snapshot.skill_id)
                    and skills[snapshot.skill_id].skill_kind == "METHODOLOGY"
                ],
            },
            "Solver State": {
                "current_phase": state.current_phase if state else run.current_phase,
                "confirmed_facts": state.confirmed_facts_json if state else [],
                "rejected_paths": state.rejected_paths_json if state else [],
                "active_hypotheses": state.active_hypotheses_json if state else [],
                "previous_action_fingerprints": dict(list((state.action_fingerprints_json if state else {}).items())[-5:]),
                "active_skill_ids": sorted(active_skill_ids),
                "no_progress_count": state.no_progress_count if state else 0,
                "last_progress_at": state.last_progress_at.isoformat() if state and state.last_progress_at else None,
                "last_action": {
                    "tool_name": last_call.tool_name,
                    "status": last_call.status,
                    "arguments": last_call.arguments_json,
                }
                if last_call
                else None,
                "last_result_classification": last_observation.facts_json if last_observation else None,
            },
            "Challenge Facts": {
                "id": challenge.id,
                "name": challenge.name,
                "description": challenge.description,
                "challenge_type": challenge.challenge_type,
                "target_url": challenge.target_url,
                "allowed_hosts": challenge.allowed_hosts,
                "flag_pattern": challenge.flag_pattern,
                "conversation_summary": run.conversation_summary,
            },
            "Recent Observations": [
                {"summary": item.summary, "facts": item.facts_json, "created_at": item.created_at.isoformat()}
                for item in reversed(observations)
            ],
            "Artifacts": [
                {
                    "path": item.file_path,
                    "type": item.artifact_type,
                    "summary": item.summary,
                    "sha256": item.sha256,
                }
                for item in reversed(artifacts)
            ],
            "Ranked Hypotheses": self._ordered_hypotheses(hypotheses),
            "Rejected Paths": state.rejected_paths_json if state else [],
            "Previous Action Fingerprints": state.action_fingerprints_json if state else {},
            "Available Tool Schemas": tool_schema,
            "Active Specialist Skills": active_skill_details,
            "Candidate Specialist Skill Summaries": candidate_skill_summaries,
            "Skill Recommendations": skill_recommendations,
            "Challenge Lesson": lessons,
            "Remaining Budget": {
                "agent_steps": max(run.max_agent_steps - run.agent_step_count, 0),
                "tool_calls": max(run.max_tool_calls - run.tool_call_count, 0),
                "runtime_seconds": max(
                    run.max_runtime_seconds
                    - (
                        int((datetime.now(UTC) - started_at).total_seconds())
                        if started_at
                        else 0
                    ),
                    0,
                ),
            },
            "Flag Candidates": [
                {"candidate_shape": re.sub(r"(?i)flag\{.*?\}", "flag{<dynamic>}", item.candidate), "verified": item.verified, "review_state": item.review_state, "pattern_matched": item.pattern_matched}
                for item in (await session.scalars(select(FlagCandidate).where(FlagCandidate.run_id == run.id))).all()
            ],
        }
        serialized = json.dumps(context, ensure_ascii=False)
        budget = 48_000
        if len(serialized) > budget:
            # Preserve current state, latest evidence and schemas; discard oldest
            # history before asking the provider for another action.
            context["Recent Observations"] = context["Recent Observations"][-4:]
            context["Artifacts"] = context["Artifacts"][-4:]
            context["Challenge Lesson"] = {key: value[:6] for key, value in lessons.items()}
            serialized = json.dumps(context, ensure_ascii=False)
        context["Context Budget"] = {"max_chars": budget, "used_chars": len(serialized), "approx_input_tokens": len(serialized) // 4, "trimmed": len(json.dumps(context, ensure_ascii=False)) > budget}
        # The model receives one system message with the core prompt and one user message
        # with ordered JSON context.
        messages = [
            {
                "role": "system",
                "content": (
                    f"{CORE_PROMPT}\n\n"
                    f"Role snapshot:\n{json.dumps(run.role_snapshot_json or {}, ensure_ascii=False)}\n\n"
                    "Return exactly one JSON action and keep the response grounded in the ordered context."
                ),
            },
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ]
        if latest_tool_view:
            messages.append(
                {
                    "role": "user",
                    "content": "The latest tool result is below. Consume it before selecting another action.\n"
                    + json.dumps(latest_tool_view, ensure_ascii=False),
                }
            )
        return messages


context_builder = ContextBuilder()
