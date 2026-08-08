from __future__ import annotations

from app.solver.blackboard import BlackboardState
from app.solver.completion import CompletionStatus, SolverCompletionEvaluator


class EvidenceAuthority:
    def __init__(self, valid: bool) -> None:
        self.valid = valid

    def verify_refs(self, refs):
        return self.valid and list(refs) == ["evidence-1"]


def test_completion_gate_rejects_information_only_facts() -> None:
    state = BlackboardState(run_id="completion-test", knowledge={"verified_facts": [{"type": "DATABASE_DISCOVERED", "verified": True}]})
    decision = SolverCompletionEvaluator().evaluate(state, evidence_authority=EvidenceAuthority(True))
    assert decision.decision is CompletionStatus.UNSOLVED
    assert decision.allowed is False


def test_completion_gate_requires_verified_finding_and_valid_evidence() -> None:
    state = BlackboardState(
        run_id="completion-test",
        knowledge={
            "findings": [{
                "type": "VERIFIED_SQL_INJECTION_FINDING",
                "verified": True,
                "validation_status": "passed",
                "evidence_refs": ["evidence-1"],
            }]
        }
    )
    decision = SolverCompletionEvaluator().evaluate(state, evidence_authority=EvidenceAuthority(True))
    assert decision.decision is CompletionStatus.SOLVED
    assert decision.evidence_checked is True
