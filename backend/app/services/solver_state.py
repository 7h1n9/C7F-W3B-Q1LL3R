import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.run import FlagCandidate, Hypothesis, SolveRun
from app.models.skill import RunSkillSnapshot, Skill
from app.models.solver_state import SolverState
from app.services.attack_chain import build_attack_chain, reduce_capability


def _phase_for(challenge_type: str) -> str:
    return "BASELINE" if challenge_type == "TRAFFIC_ANALYSIS" else "INTAKE"


def _unique_json_list(items: list[dict], entry: dict) -> list[dict]:
    signature = json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str)
    if signature in {
        json.dumps(item, ensure_ascii=False, sort_keys=True, default=str) for item in items
    }:
        return items
    return [*items, entry]


class SolverStateService:
    async def load(self, session: AsyncSession, run_id: str) -> SolverState | None:
        return await session.scalar(select(SolverState).where(SolverState.run_id == run_id))

    async def initialize(
        self,
        session: AsyncSession,
        run: SolveRun,
        challenge_type: str,
        active_skill_ids: list[str],
        challenge_name: str = "",
        challenge_description: str = "",
    ) -> SolverState:
        state = await self.load(session, run.id)
        if state:
            if not state.attack_chain_plan_json:
                state.attack_chain_plan_json = build_attack_chain(challenge_name, challenge_description)
                await session.commit()
            return state
        state = SolverState(
            run_id=run.id,
            current_phase=_phase_for(challenge_type),
            active_skill_ids_json=sorted(set(active_skill_ids)),
            last_progress_at=datetime.now(UTC),
            run_plan_json={
                "current_goal": "Understand the authorized challenge and find a verified flag",
                "current_phase": _phase_for(challenge_type),
                "attack_surface": [], "confirmed_capabilities": [], "open_questions": [],
                "hypothesis_queue": [], "current_experiment": None, "next_actions": [],
                "exit_conditions": ["verified flag", "explicit unrecoverable blocker"],
            },
            capability_ledger_json={},
            attack_chain_plan_json=build_attack_chain(challenge_name, challenge_description),
            experiment_dimensions_json=[],
        )
        session.add(state)
        await session.commit()
        await session.refresh(state)
        return state

    async def sync_from_run(self, session: AsyncSession, run: SolveRun) -> SolverState | None:
        verified = await session.scalar(select(FlagCandidate.id).where(FlagCandidate.run_id == run.id, FlagCandidate.verified, FlagCandidate.review_state == "VALID"))
        if verified and run.status != "COMPLETED_SOLVED":
            run.status = "COMPLETED_SOLVED"
            run.current_phase = "COMPLETED_SOLVED"
            run.thread_invalidated = True
        state = await self.load(session, run.id)
        if not state:
            return None
        state.current_phase = run.current_phase
        if run.status == "COMPLETED_SOLVED":
            state.current_phase = "COMPLETED_SOLVED"
        await session.commit()
        return state

    async def check_consistency(self, session: AsyncSession, run: SolveRun) -> dict:
        state = await self.load(session, run.id)
        if not state:
            return {"ok": False, "code": "STATE_INVARIANT_VIOLATION", "fields": ["SolverState"]}
        expected = "COMPLETED_SOLVED" if run.status == "COMPLETED_SOLVED" else run.current_phase
        fields = []
        if state.current_phase != expected:
            fields.append("SolverState.current_phase")
        if run.status == "COMPLETED_SOLVED" and not run.thread_invalidated:
            fields.append("Run.thread_invalidated")
        return {"ok": not fields, "code": None if not fields else "STATE_INVARIANT_VIOLATION", "fields": fields}

    async def sync_hypotheses(self, session: AsyncSession, run_id: str) -> list[dict]:
        state = await self.load(session, run_id)
        if not state:
            return []
        hypotheses = list(
            (await session.scalars(select(Hypothesis).where(Hypothesis.run_id == run_id))).all()
        )
        state.active_hypotheses_json = [
            {
                "id": item.id,
                "category": item.category,
                "statement": item.title,
                "confidence": item.confidence,
                "status": item.status,
            }
            for item in hypotheses
        ]
        await session.commit()
        return state.active_hypotheses_json

    async def activate_skill(self, session: AsyncSession, run_id: str, skill_id: str) -> bool:
        state = await self.load(session, run_id)
        if not state:
            return False
        skill = await session.get(Skill, skill_id)
        if not skill or not skill.enabled:
            return False
        active_ids = set(state.active_skill_ids_json or [])
        if skill.id in active_ids:
            return False
        snapshot = await session.scalar(
            select(RunSkillSnapshot).where(
                RunSkillSnapshot.run_id == run_id, RunSkillSnapshot.skill_id == skill.id
            )
        )
        if snapshot is None:
            snapshot = RunSkillSnapshot(
                run_id=run_id,
                skill_id=skill.id,
                skill_name=skill.name,
                skill_version=skill.version,
                content_snapshot=skill.content_markdown,
                allowed_tools_snapshot=skill.allowed_tools,
                config_snapshot={},
                priority=1000,
            )
            session.add(snapshot)
        active_ids.add(skill.id)
        state.active_skill_ids_json = sorted(active_ids)
        await session.commit()
        return True

    async def deactivate_skill(self, session: AsyncSession, run_id: str, skill_id: str) -> bool:
        state = await self.load(session, run_id)
        if not state:
            return False
        skill = await session.get(Skill, skill_id)
        if not skill or not skill.enabled:
            return False
        if skill.skill_kind in {"CORE", "METHODOLOGY"}:
            return False
        active_ids = set(state.active_skill_ids_json or [])
        if skill.id not in active_ids:
            return False
        active_ids.remove(skill.id)
        state.active_skill_ids_json = sorted(active_ids)
        await session.commit()
        return True

    async def record_skill_recommendations(self, session: AsyncSession, run_id: str, entries: list[dict]) -> None:
        state = await self.load(session, run_id)
        if not state:
            return
        state.skill_recommendations_json = entries
        await session.commit()

    async def record_fingerprint(
        self,
        session: AsyncSession,
        run_id: str,
        fingerprint: str,
        *,
        tool_name: str,
        arguments: dict,
        status: str,
        retry_reason: str | None = None,
    ) -> None:
        state = await self.load(session, run_id)
        if not state:
            return
        state.action_fingerprints_json = {
            **(state.action_fingerprints_json or {}),
            fingerprint: {
                "tool_name": tool_name,
                "arguments": arguments,
                "status": status,
                "retry_reason": retry_reason,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        }
        await session.commit()

    async def record_confirmation(self, session: AsyncSession, run_id: str, entry: dict) -> bool:
        state = await self.load(session, run_id)
        if not state:
            return False
        before = len(state.confirmed_facts_json or [])
        state.confirmed_facts_json = _unique_json_list(state.confirmed_facts_json or [], entry)
        changed = len(state.confirmed_facts_json) != before
        if changed:
            state.no_progress_count = 0
            state.investigation_no_progress_count = 0
            state.duplicate_action_streak = 0
            state.last_progress_at = datetime.now(UTC)
        await session.commit()
        return changed

    async def record_rejected_path(self, session: AsyncSession, run_id: str, entry: dict) -> bool:
        state = await self.load(session, run_id)
        if not state:
            return False
        before = len(state.rejected_paths_json or [])
        state.rejected_paths_json = _unique_json_list(state.rejected_paths_json or [], entry)
        changed = len(state.rejected_paths_json) != before
        if changed:
            state.last_progress_at = datetime.now(UTC)
        await session.commit()
        return changed

    async def record_progress(self, session: AsyncSession, run_id: str, made_progress: bool) -> int:
        state = await self.load(session, run_id)
        if not state:
            return 0
        if made_progress:
            state.no_progress_count = 0
            state.investigation_no_progress_count = 0
            state.last_progress_at = datetime.now(UTC)
        else:
            state.no_progress_count += 1
            state.investigation_no_progress_count += 1
        await session.commit()
        return state.no_progress_count

    async def record_control_rejection(self, session: AsyncSession, run_id: str, entry: dict) -> None:
        """Persist a tool/control failure without treating it as vulnerability evidence."""
        state = await self.load(session, run_id)
        if not state:
            return
        state.last_result_classification = "CONTROL_REJECTION"
        state.investigation_no_progress_count += 1
        state.control_rejection_streak += 1
        if str(entry.get("code") or "").upper() in {"TOOL_INVALID_ARGUMENT", "SCHEMA_VALIDATION_FAILED"}:
            state.schema_error_streak += 1
        if str(entry.get("code") or "").upper() == "DUPLICATE_ACTION":
            state.duplicate_action_streak += 1
        state.rejected_paths_json = _unique_json_list(
            state.rejected_paths_json or [], {**entry, "classification": "CONTROL_REJECTION"}
        )
        await session.commit()

    async def record_action_outcome(self, session: AsyncSession, run_id: str, *, progress: bool, duplicate: bool = False) -> dict:
        state = await self.load(session, run_id)
        if not state:
            return {}
        if progress:
            state.duplicate_action_streak = 0
            state.control_rejection_streak = 0
            state.schema_error_streak = 0
            state.degraded_action_streak = 0
        elif duplicate:
            state.duplicate_action_streak += 1
        state.investigation_no_progress_count = state.no_progress_count
        if state.duplicate_action_streak >= 2:
            state.force_plan_action = 1
        await session.commit()
        return {
            "duplicate_action_streak": state.duplicate_action_streak,
            "control_rejection_streak": state.control_rejection_streak,
            "schema_error_streak": state.schema_error_streak,
            "investigation_no_progress_count": state.investigation_no_progress_count,
            "force_plan_action": bool(state.force_plan_action),
            "force_automation": state.duplicate_action_streak >= 3,
        }

    async def record_result_classification(self, session: AsyncSession, run_id: str, classification: str) -> None:
        state = await self.load(session, run_id)
        if state:
            state.last_result_classification = classification
            await session.commit()

    async def set_run_plan(self, session: AsyncSession, run_id: str, plan: dict) -> None:
        state = await self.load(session, run_id)
        if not state:
            return
        state.run_plan_json = dict(plan)
        await session.commit()

    async def record_decision_card(self, session: AsyncSession, run_id: str, card: dict) -> None:
        state = await self.load(session, run_id)
        if not state:
            return
        state.last_decision_card_json = dict(card)
        await session.commit()

    async def record_experiment(self, session: AsyncSession, run_id: str, experiment: dict) -> None:
        state = await self.load(session, run_id)
        if not state:
            return
        state.last_experiment_json = dict(experiment)
        dimensions = set(state.experiment_dimensions_json or [])
        if experiment.get("tool"):
            dimensions.add(f"tool:{experiment['tool']}")
        if experiment.get("arguments", {}).get("method"):
            dimensions.add(f"method:{experiment['arguments']['method']}")
        if experiment.get("arguments", {}).get("url"):
            dimensions.add(f"path:{str(experiment['arguments']['url']).split('?', 1)[0]}")
        state.experiment_dimensions_json = sorted(dimensions)
        state.last_result_classification = str(experiment.get("result_classification") or "UNKNOWN")
        await session.commit()

    async def record_file_read(self, session: AsyncSession, run_id: str, *, path: str, start_line: int, end_line: int, content_sha256: str) -> None:
        state = await self.load(session, run_id)
        if not state:
            return
        state.read_files_json = sorted(set([*(state.read_files_json or []), path]))
        ranges = [item for item in (state.read_ranges_json or []) if not (item.get("path") == path and item.get("start_line") == start_line and item.get("end_line") == end_line)]
        state.read_ranges_json = [*ranges, {"path": path, "start_line": start_line, "end_line": end_line}]
        state.content_hashes_json = {**(state.content_hashes_json or {}), path: content_sha256}
        await session.commit()

    async def record_capability(self, session: AsyncSession, run_id: str, capability: str, *, evidence: dict | None = None) -> None:
        state = await self.load(session, run_id)
        if not state or not capability:
            return
        ledger, plan, _current_node = reduce_capability(
            state.capability_ledger_json or {}, state.attack_chain_plan_json or {}, capability, evidence
        )
        state.capability_ledger_json = ledger
        state.attack_chain_plan_json = plan
        state.no_progress_count = 0
        state.investigation_no_progress_count = 0
        state.duplicate_action_streak = 0
        state.last_progress_at = datetime.now(UTC)
        await session.commit()

    async def record_finish_rejection(self, session: AsyncSession, run_id: str, missing: list[str]) -> int:
        state = await self.load(session, run_id)
        if not state:
            return 0
        state.finish_rejection_count += 1
        state.force_plan_action = 1 if state.finish_rejection_count >= 2 else state.force_plan_action
        state.last_result_classification = "CONTROL_REJECTION"
        state.rejected_paths_json = _unique_json_list(
            state.rejected_paths_json or [],
            {"source": "finish_gate", "code": "FINISH_PREMATURE", "missing_requirements": missing, "classification": "CONTROL_REJECTION"},
        )
        await session.commit()
        return state.finish_rejection_count

    async def require_plan_action(self, session: AsyncSession, run_id: str, required: bool = True) -> None:
        state = await self.load(session, run_id)
        if state:
            state.force_plan_action = 1 if required else 0
            await session.commit()


solver_state_service = SolverStateService()
