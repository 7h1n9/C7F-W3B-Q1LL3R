"""Bounded unattended solve/recover loop for Codex SDK runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

RECOVERABLE_STREAM_CODES = {"CODEX_STREAM_ERROR", "CODEX_STREAM_INTERRUPTED", "CODEX_STREAM_INTERRUPTED"}


@dataclass
class RootCauseReport:
    category: str
    code: str
    evidence: dict[str, Any] = field(default_factory=dict)
    repair: str = ""


@dataclass
class CampaignResult:
    status: str
    rounds: int
    attempts: int
    recoveries: int
    reports: list[RootCauseReport] = field(default_factory=list)
    fresh_reproduction: bool = False


def classify_failure(code: str | None, message: str | None = None) -> RootCauseReport:
    normalized = str(code or "ENGINE_ERROR").upper()
    text = str(message or "")
    if normalized in RECOVERABLE_STREAM_CODES or any(item in text.lower() for item in ("tls handshake eof", "stream disconnected", "request timed out")):
        return RootCauseReport("STREAM_FAILURE", normalized, {"message": text}, "persist EvidenceSnapshot, checkpoint and batch parameters; resume the same plan")
    if normalized.startswith("MCP_") or normalized in {"TOOL_INVALID_ARGUMENT", "SCHEMA_VALIDATION_FAILED"}:
        return RootCauseReport("TOOL_SCHEMA_FAILURE", normalized, {"message": text}, "repair RequestSpec/schema adaptation and rerun preflight")
    if normalized.startswith("RUNNER") or normalized in {"SCRIPT_RESULT_MISSING", "SCRIPT_RESULT_INVALID", "TARGET_RATE_LIMITED"}:
        return RootCauseReport("RUNNER_FAILURE", normalized, {"message": text}, "repair bounded Job state/queue/retry handling")
    if "deadlock" in text.lower() or "database" in normalized or "migration" in normalized:
        return RootCauseReport("DATABASE_FAILURE", normalized, {"message": text}, "rollback/compact internal trace and retry the transaction")
    return RootCauseReport("METHOD_FAILURE", normalized, {"message": text}, "repair methodology/policy and preserve the evidence ledger")


class UnattendedSolveCampaign:
    """Run bounded rounds without asking for user intervention.

    The callbacks keep orchestration testable and ensure this service never
    invents challenge source facts or bypasses the normal Run state machine.
    """

    def __init__(self, *, max_rounds: int = 3, max_recoveries: int = 5) -> None:
        self.max_rounds = max_rounds
        self.max_recoveries = max_recoveries

    async def execute(
        self,
        run_once: Callable[[int], Awaitable[dict[str, Any]]],
        repair_once: Callable[[RootCauseReport], Awaitable[None]],
        fresh_reproduce: Callable[[], Awaitable[bool]],
    ) -> CampaignResult:
        reports: list[RootCauseReport] = []
        recoveries = 0
        attempts = 0
        for round_number in range(1, self.max_rounds + 1):
            attempts += 1
            outcome = await run_once(round_number)
            if outcome.get("status") == "COMPLETED_SOLVED" and int(outcome.get("verified_flag_count") or 0) >= 1:
                reproduced = await fresh_reproduce()
                return CampaignResult("COMPLETED", round_number, attempts, recoveries, reports, reproduced)
            report = classify_failure(outcome.get("error_code"), outcome.get("error_message"))
            reports.append(report)
            if recoveries >= self.max_recoveries:
                break
            recoveries += 1
            await repair_once(report)
        return CampaignResult("FAILED", self.max_rounds, attempts, recoveries, reports, False)


unattended_solve_campaign = UnattendedSolveCampaign()
