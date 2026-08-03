"""Terminal self-review: a WP is allowed only after bounded work is exhausted."""

from sqlalchemy import select

from app.models.challenge import Challenge
from app.models.multi_agent import AgentTask, PlannerProposal, VerifiedFact
from app.models.solver_state import SolverState
from app.services.user_input_resume_guard import check_user_input_resume


class SolverSelfReview:
    async def review(self, session, run, *, explicit_finish: bool = False) -> dict:
        if explicit_finish:
            return {"status": "PASS", "reasons": ["USER_EXPLICIT_FINISH"]}
        checkpoint = dict(run.recovery_checkpoint_json or {})
        resume_guard = await check_user_input_resume(session, run.id)
        if not resume_guard.get("ok"):
            return {"status": "FAIL", "reasons": ["USER_INPUT_RESUME_NO_PROGRESS"], "details": resume_guard}
        state = await session.scalar(select(SolverState).where(SolverState.run_id == run.id))
        challenge = await session.get(Challenge, run.challenge_id)
        metadata = (challenge.metadata_json or {}) if challenge else {}
        asset_mysql = str(metadata.get("adapter") or "").lower() == "asset_warranty" and str(metadata.get("dbms") or "").lower() == "mysql"
        if asset_mysql:
            facts = set((await session.scalars(select(VerifiedFact.fact_key).where(VerifiedFact.run_id == run.id, VerifiedFact.promotion_status == "VERIFIED"))).all())
            essential = {"asset_warranty.current_database", "asset_warranty.mysql_user_tables", "asset_warranty.mysql_candidate_columns"}
            progress = (state.capability_ledger_json or {}).get("metadata_progress", {}) if state else {}
            blocked = {stage for stage in ("database", "tables", "columns") if str((progress.get(stage) or {}).get("status") or "").upper() == "BLOCKED"}
            if not essential <= facts and len(blocked) < 3:
                return {"status": "FAIL", "reasons": ["ESSENTIAL_STAGE_REMAINS_EXECUTABLE"]}
            if any(str((progress.get(stage) or {}).get("status") or "PENDING").upper() != "BLOCKED" for stage in ("database", "tables", "columns")):
                return {"status": "FAIL", "reasons": ["METADATA_STAGE_REMAINS_UNBLOCKED"]}
        if checkpoint.get("resume_reason") == "USER_INPUT_RECEIVED" and not checkpoint.get("planner_context_consumed"):
            return {"status": "FAIL", "reasons": ["USER_INPUT_NOT_CONTINUED"]}
        if checkpoint.get("payload_strategies_exhausted") is not True and checkpoint.get("choose_new_payload_family"):
            return {"status": "FAIL", "reasons": ["PAYLOAD_FAMILY_REMAINS"]}
        history = list(checkpoint.get("payload_strategy_history") or [])
        if history:
            open_families = [item for item in history if item.get("status") != "BLOCKED"]
            if open_families:
                return {"status": "FAIL", "reasons": ["PAYLOAD_FAMILY_REMAINS"]}
        return {"status": "PASS", "reasons": []}


solver_self_review = SolverSelfReview()
