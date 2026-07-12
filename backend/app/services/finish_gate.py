from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.challenge import Challenge
from app.models.run import FlagCandidate, SolveRun
from app.models.solver_state import SolverState


class FinishGate:
    async def evaluate(
        self,
        session: AsyncSession,
        run: SolveRun,
        challenge: Challenge,
        candidate_verified: bool = False,
    ) -> tuple[bool, str, str]:
        state = await session.scalar(select(SolverState).where(SolverState.run_id == run.id))
        if not state:
            return False, "PREMATURE_FINISH", "Solver state is unavailable."

        confirmed_sources = {str(item.get("source")) for item in (state.confirmed_facts_json or [])}
        rejected_paths = state.rejected_paths_json or []
        active_hypotheses = state.active_hypotheses_json or []
        unresolved_flags = list(
            (
                await session.scalars(
                    select(FlagCandidate).where(
                        FlagCandidate.run_id == run.id,
                        FlagCandidate.review_state == "OPEN",
                    )
                )
            ).all()
        )

        if challenge.challenge_type == "TRAFFIC_ANALYSIS":
            required = {"pcap_metadata", "pcap_protocols", "pcap_query"}
            if not required.issubset(confirmed_sources):
                return (
                    False,
                    "PREMATURE_FINISH",
                    "Traffic runs require PCAP metadata, protocol mapping, and at least one narrow query before finishing.",
                )
        else:
            if not confirmed_sources.intersection({"http_request", "file_read", "file_search"}):
                return (
                    False,
                    "PREMATURE_FINISH",
                    "Web runs require baseline evidence from the target or source material before finishing.",
                )
        if not rejected_paths:
            return (
                False,
                "PREMATURE_FINISH",
                "At least one rejected path is required before finishing.",
            )
        if not active_hypotheses:
            return (
                False,
                "PREMATURE_FINISH",
                "At least one hypothesis must be recorded before finishing.",
            )
        if unresolved_flags:
            return (
                False,
                "PREMATURE_FINISH",
                "Unresolved flag candidates remain.",
            )
        return True, "OK", "Finish gate passed."


finish_gate = FinishGate()
