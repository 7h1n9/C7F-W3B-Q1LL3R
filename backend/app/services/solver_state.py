import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.run import Hypothesis, SolveRun
from app.models.skill import RunSkillSnapshot, Skill
from app.models.solver_state import SolverState


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
    ) -> SolverState:
        state = await self.load(session, run.id)
        if state:
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
        )
        session.add(state)
        await session.commit()
        await session.refresh(state)
        return state

    async def sync_from_run(self, session: AsyncSession, run: SolveRun) -> SolverState | None:
        state = await self.load(session, run.id)
        if not state:
            return None
        state.current_phase = run.current_phase
        await session.commit()
        return state

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
            state.last_progress_at = datetime.now(UTC)
        else:
            state.no_progress_count += 1
        await session.commit()
        return state.no_progress_count

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
        ledger = dict(state.capability_ledger_json or {})
        if capability not in ledger:
            ledger[capability] = {"confirmed": True, "evidence": evidence or {}, "confirmed_at": datetime.now(UTC).isoformat()}
            state.capability_ledger_json = ledger
            await session.commit()


solver_state_service = SolverStateService()
