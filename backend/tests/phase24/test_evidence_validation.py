from __future__ import annotations

from app.solver.blackboard import BlackboardState
from app.solver.completion import CompletionStatus, SolverCompletionEvaluator


class InvalidEvidenceAuthority:
    def verify_refs(self, refs):
        return False


def test_invalid_evidence_cannot_cross_completion_gate() -> None:
    state = BlackboardState(
        run_id="evidence-test",
        knowledge={
            "findings": [{
                "verified": True,
                "validation_status": "passed",
                "evidence_refs": ["missing-evidence"],
            }]
        }
    )
    decision = SolverCompletionEvaluator().evaluate(state, evidence_authority=InvalidEvidenceAuthority())
    assert decision.decision is CompletionStatus.UNSOLVED
    assert decision.reason_code == "EVIDENCE_INVALID"
    assert decision.evidence_checked is True
