"""Evidence-driven completion decisions for the Solver v2 runtime."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from .blackboard import BlackboardState


class CompletionStatus(StrEnum):
    SOLVED = "SOLVED"
    UNSOLVED = "UNSOLVED"
    WAITING = "WAITING"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class CompletionDecision:
    decision: CompletionStatus
    allowed: bool
    reason_code: str
    reason: str
    missing_requirements: list[str] = field(default_factory=list)
    evidence_checked: bool = False


class EvidenceAuthority(Protocol):
    """Read-only boundary used by the completion evaluator."""

    def verify_refs(self, evidence_refs: Sequence[str]) -> bool: ...


class SolverCompletionEvaluator:
    """Evaluate Solver-owned knowledge without owning Evidence or Run state."""

    def evaluate(
        self,
        blackboard_state: BlackboardState,
        *,
        evidence_authority: EvidenceAuthority,
    ) -> CompletionDecision:
        knowledge = blackboard_state.knowledge
        control = blackboard_state.control

        if self._approval_required(knowledge, control):
            return self._decision(
                CompletionStatus.WAITING,
                "APPROVAL_REQUIRED",
                "Completion is waiting for required approval or user input.",
                ["approval or user input"],
            )

        blockers = self._blockers(knowledge, control)
        if blockers:
            return self._decision(
                CompletionStatus.BLOCKED,
                "COMPLETION_BLOCKED",
                "Completion is blocked by unresolved runtime or security conditions.",
                blockers,
            )

        findings = self._findings(knowledge.get("findings"))
        if not findings:
            return self._decision(
                CompletionStatus.UNSOLVED,
                "FINDING_REQUIRED",
                "A verified Finding is required before the Solver can be marked solved.",
                ["verified finding"],
            )

        for finding in findings:
            decision = self._evaluate_finding(finding, evidence_authority)
            if decision.decision is CompletionStatus.SOLVED:
                return decision

        return self._evaluate_finding(findings[0], evidence_authority)

    @staticmethod
    def _evaluate_finding(
        finding: Mapping[str, Any],
        evidence_authority: EvidenceAuthority,
    ) -> CompletionDecision:
        if finding.get("verified") is not True:
            return CompletionDecision(
                decision=CompletionStatus.UNSOLVED,
                allowed=False,
                reason_code="FINDING_NOT_VERIFIED",
                reason="The Finding has not been verified.",
                missing_requirements=["verified finding"],
            )

        if str(finding.get("validation_status") or "").casefold() != "passed":
            return CompletionDecision(
                decision=CompletionStatus.UNSOLVED,
                allowed=False,
                reason_code="VALIDATION_NOT_PASSED",
                reason="The Finding does not have a passed validation status.",
                missing_requirements=["validation_status=passed"],
            )

        evidence_refs = SolverCompletionEvaluator._evidence_refs(finding.get("evidence_refs"))
        if not evidence_refs:
            return CompletionDecision(
                decision=CompletionStatus.UNSOLVED,
                allowed=False,
                reason_code="FINDING_EVIDENCE_MISSING",
                reason="The verified Finding does not reference Evidence.",
                missing_requirements=["finding evidence_refs"],
            )

        try:
            evidence_valid = bool(evidence_authority.verify_refs(evidence_refs))
        except Exception:
            return CompletionDecision(
                decision=CompletionStatus.BLOCKED,
                allowed=False,
                reason_code="EVIDENCE_AUTHORITY_UNAVAILABLE",
                reason="Evidence authority could not verify the Finding references.",
                missing_requirements=["Evidence authority verification"],
                evidence_checked=False,
            )

        if not evidence_valid:
            return CompletionDecision(
                decision=CompletionStatus.UNSOLVED,
                allowed=False,
                reason_code="EVIDENCE_INVALID",
                reason="One or more Finding Evidence references are invalid.",
                missing_requirements=["valid Finding Evidence references"],
                evidence_checked=True,
            )

        return CompletionDecision(
            decision=CompletionStatus.SOLVED,
            allowed=True,
            reason_code="COMPLETION_GATE_PASSED",
            reason="The Finding is verified, validated, and supported by valid Evidence.",
            evidence_checked=True,
        )

    @staticmethod
    def _findings(value: Any) -> list[Mapping[str, Any]]:
        if isinstance(value, Mapping):
            return [value]
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, Mapping)]

    @staticmethod
    def _evidence_refs(value: Any) -> list[str]:
        if not isinstance(value, (list, tuple, set)):
            return []
        return [str(item) for item in value if str(item).strip()]

    @staticmethod
    def _approval_required(knowledge: Mapping[str, Any], control: Mapping[str, Any]) -> bool:
        if knowledge.get("approval_required") is True or control.get("approval_required") is True:
            return True
        if str(control.get("last_action_authorization") or "").upper() == "REQUIRE_APPROVAL":
            return True
        return any(
            str(item.get("type") or "") == "ACTION_APPROVAL_REQUIRED"
            for item in (knowledge.get("hypotheses") or [])
            if isinstance(item, Mapping)
        )

    @staticmethod
    def _blockers(knowledge: Mapping[str, Any], control: Mapping[str, Any]) -> list[str]:
        blockers: list[str] = []
        for source in (knowledge.get("blockers"), control.get("blockers"), control.get("unresolved_blockers")):
            if isinstance(source, Mapping):
                blockers.extend(str(key) for key, value in source.items() if value)
            elif isinstance(source, (list, tuple, set)):
                blockers.extend(str(item) for item in source if str(item).strip())
            elif source:
                blockers.append(str(source))
        return list(dict.fromkeys(blockers))

    @staticmethod
    def _decision(
        decision: CompletionStatus,
        reason_code: str,
        reason: str,
        missing_requirements: list[str],
    ) -> CompletionDecision:
        return CompletionDecision(
            decision=decision,
            allowed=False,
            reason_code=reason_code,
            reason=reason,
            missing_requirements=missing_requirements,
        )
