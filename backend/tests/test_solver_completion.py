from __future__ import annotations

from copy import deepcopy

from app.solver.blackboard import BlackboardState
from app.solver.completion import CompletionStatus, SolverCompletionEvaluator


class FakeEvidenceAuthority:
    def __init__(self, valid: bool = True) -> None:
        self.valid = valid
        self.checked: list[str] = []

    def verify_refs(self, evidence_refs) -> bool:
        self.checked = list(evidence_refs)
        return self.valid


def state(*, knowledge: dict | None = None, control: dict | None = None) -> BlackboardState:
    return BlackboardState(
        run_id="completion-test-run",
        phase="REPORTING",
        knowledge=knowledge or {},
        control=control or {},
    )


def verified_finding(**overrides) -> dict:
    return {
        "id": "finding-1",
        "verified": True,
        "validation_status": "passed",
        "evidence_refs": ["evidence-1"],
        **overrides,
    }


def test_verified_finding_with_valid_evidence_is_solved() -> None:
    authority = FakeEvidenceAuthority()
    decision = SolverCompletionEvaluator().evaluate(
        state(knowledge={"findings": [verified_finding()]}),
        evidence_authority=authority,
    )

    assert decision.decision is CompletionStatus.SOLVED
    assert decision.allowed is True
    assert decision.evidence_checked is True
    assert authority.checked == ["evidence-1"]


def test_no_finding_is_unsolved() -> None:
    decision = SolverCompletionEvaluator().evaluate(
        state(knowledge={"verified_facts": [{"type": "HTTP_ENDPOINT_FOUND", "verified": True}]}),
        evidence_authority=FakeEvidenceAuthority(),
    )

    assert decision.decision is CompletionStatus.UNSOLVED
    assert decision.reason_code == "FINDING_REQUIRED"


def test_database_discovery_only_is_unsolved() -> None:
    decision = SolverCompletionEvaluator().evaluate(
        state(knowledge={"verified_facts": [{"type": "DATABASE_DISCOVERED", "verified": True}]}),
        evidence_authority=FakeEvidenceAuthority(),
    )

    assert decision.decision is CompletionStatus.UNSOLVED


def test_schema_discovery_only_is_unsolved() -> None:
    decision = SolverCompletionEvaluator().evaluate(
        state(knowledge={"verified_facts": [{"type": "SCHEMA_DISCOVERED", "verified": True}]}),
        evidence_authority=FakeEvidenceAuthority(),
    )

    assert decision.decision is CompletionStatus.UNSOLVED


def test_finding_without_evidence_is_unsolved() -> None:
    decision = SolverCompletionEvaluator().evaluate(
        state(knowledge={"findings": [verified_finding(evidence_refs=[])]}),
        evidence_authority=FakeEvidenceAuthority(),
    )

    assert decision.decision is CompletionStatus.UNSOLVED
    assert decision.reason_code == "FINDING_EVIDENCE_MISSING"


def test_invalid_evidence_is_unsolved() -> None:
    decision = SolverCompletionEvaluator().evaluate(
        state(knowledge={"findings": [verified_finding()]}),
        evidence_authority=FakeEvidenceAuthority(valid=False),
    )

    assert decision.decision is CompletionStatus.UNSOLVED
    assert decision.reason_code == "EVIDENCE_INVALID"
    assert decision.evidence_checked is True


def test_blocker_is_blocked() -> None:
    decision = SolverCompletionEvaluator().evaluate(
        state(
            knowledge={"findings": [verified_finding()], "blockers": ["TARGET_UNREACHABLE"]},
        ),
        evidence_authority=FakeEvidenceAuthority(),
    )

    assert decision.decision is CompletionStatus.BLOCKED
    assert decision.reason_code == "COMPLETION_BLOCKED"


def test_approval_required_is_waiting() -> None:
    decision = SolverCompletionEvaluator().evaluate(
        state(
            knowledge={"findings": [verified_finding()]},
            control={"approval_required": True},
        ),
        evidence_authority=FakeEvidenceAuthority(),
    )

    assert decision.decision is CompletionStatus.WAITING
    assert decision.reason_code == "APPROVAL_REQUIRED"


def test_evaluator_does_not_modify_blackboard_input() -> None:
    current = state(knowledge={"findings": [verified_finding()]})
    before = deepcopy(current.model_dump(mode="python"))

    SolverCompletionEvaluator().evaluate(
        current,
        evidence_authority=FakeEvidenceAuthority(),
    )

    assert current.model_dump(mode="python") == before
