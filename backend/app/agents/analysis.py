from app.agents.base import StructuredAgent
from app.schemas.multi_agent import (
    AgentRole,
    AnalysisDecision,
    AnalysisReviewContract,
    PlannerProposalContract,
)


class AnalysisAgent(StructuredAgent):
    role = AgentRole.ANALYSIS

    def review(self, proposal: PlannerProposalContract, *, decision: AnalysisDecision, question_being_tested: str = "", supporting_evidence_ids: list[str] | None = None, independent_variable: str | None = None, required_controls: dict | None = None, reason: str = "", **signals) -> AnalysisReviewContract:
        return AnalysisReviewContract(
            proposal_id=proposal.proposal_id, decision=decision, question_being_tested=question_being_tested,
            supporting_evidence_ids=supporting_evidence_ids or [], independent_variable=independent_variable,
            required_controls=required_controls or {}, reason=reason, **signals,
        )

