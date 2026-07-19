from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.challenge import Challenge
from app.models.run import FlagCandidate, SolveRun
from app.models.solver_state import SolverState


class FinishGate:
    async def evaluate_detailed(
        self,
        session: AsyncSession,
        run: SolveRun,
        challenge: Challenge,
        candidate_verified: bool = False,
        result: str | None = None,
    ) -> dict:
        """Return auditable requirements without leaking implementation details."""
        state = await session.scalar(select(SolverState).where(SolverState.run_id == run.id))
        if not state:
            return {"allowed": False, "code": "PREMATURE_FINISH", "message": "Solver state is unavailable.", "missing_requirements": ["solver state"]}

        confirmed_sources = {str(item.get("source")) for item in (state.confirmed_facts_json or []) if item.get("classification") != "CONTROL_REJECTION"}
        rejected_paths = [item for item in (state.rejected_paths_json or []) if item.get("classification") != "CONTROL_REJECTION"]
        active_hypotheses = state.active_hypotheses_json or []
        unresolved_flags = list((await session.scalars(select(FlagCandidate).where(FlagCandidate.run_id == run.id, FlagCandidate.review_state == "OPEN"))).all())
        missing: list[str] = []

        hard_blockers = {
            "TARGET_UNREACHABLE",
            "REQUIRED_ATTACHMENT_MISSING",
            "ATTACHMENT_MISSING",
            "PROVIDER_CONFIGURATION_INVALID",
            "RUNNER_CONFIGURATION_INVALID",
            "AUTHORIZATION_BOUNDARY_UNCLEAR",
            "REQUIRED_USER_SECRET_MISSING",
        }
        if result == "unsolved" and str(run.last_error_code or "") in hard_blockers:
            return {
                "allowed": False,
                "code": "WAITING_CONFIGURATION" if str(run.last_error_code).endswith("CONFIGURATION_INVALID") else "WAITING_USER",
                "message": "The run has a hard blocker and must wait for configuration or user input.",
                "missing_requirements": [str(run.last_error_code)],
            }

        if challenge.challenge_type == "TRAFFIC_ANALYSIS":
            required = {"pcap_metadata", "pcap_protocols", "pcap_query"}
            missing.extend(f"confirmed evidence: {item}" for item in sorted(required - confirmed_sources))
        elif not confirmed_sources.intersection({"http_request", "file_read", "file_search"}):
            missing.append("baseline evidence from target or source")
        if not rejected_paths:
            missing.append("one valid NEGATIVE or BLOCKED path")
        if len(active_hypotheses) < 1:
            missing.append("one hypothesis")
        if unresolved_flags:
            missing.append("review all flag candidates")

        if result == "unsolved":
            attempt_steps = int(run.attempt_agent_steps or 0)
            attempt_tools = int(run.attempt_logical_tool_calls or 0)
            run_steps = int(run.run_total_agent_steps or run.agent_step_count or 0)
            experiment_dimensions = len(state.experiment_dimensions_json or [])
            if attempt_steps < 12:
                missing.append(f"current attempt agent steps >= 12 (currently {attempt_steps})")
            if run_steps < 30:
                missing.append(f"run cumulative agent steps >= 30 (currently {run_steps})")
            if attempt_tools < 3:
                missing.append(f"current attempt valid logical tool calls >= 3 (currently {attempt_tools})")
            if len(active_hypotheses) < 2:
                missing.append(f"at least 2 hypotheses (currently {len(active_hypotheses)})")
            if experiment_dimensions < 2:
                missing.append(f"at least 2 experiment dimensions (currently {experiment_dimensions})")
            if not any(str(item.get("classification")) in {"NEGATIVE", "BLOCKED"} for item in (state.rejected_paths_json or [])):
                missing.append("one classified NEGATIVE or BLOCKED result")
            plan = state.attack_chain_plan_json or {}
            if any(item.get("status") == "READY" and item.get("priority", 0) > 0 for item in plan.get("nodes", [])):
                missing.append("no executable high-priority attack-chain node")

        if missing:
            return {
                "allowed": False,
                "code": "PREMATURE_FINISH",
                "message": "FinishGate rejected the finish request.",
                "missing_requirements": missing,
            }
        return {"allowed": True, "code": "OK", "message": "Finish gate passed.", "missing_requirements": []}

    async def evaluate(
        self,
        session: AsyncSession,
        run: SolveRun,
        challenge: Challenge,
        candidate_verified: bool = False,
        result: str | None = None,
    ) -> tuple[bool, str, str]:
        result_data = await self.evaluate_detailed(session, run, challenge, candidate_verified, result)
        return bool(result_data["allowed"]), str(result_data["code"]), str(result_data["message"])


finish_gate = FinishGate()
