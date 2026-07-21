from enum import StrEnum


class TerminalOutcome(StrEnum):
    VERIFIED_FLAG = "VERIFIED_FLAG"
    COMPLETED_SOLVED = "COMPLETED_SOLVED"
    COMPLETED_UNSOLVED = "COMPLETED_UNSOLVED"
    CANCELLED = "CANCELLED"
    FAILED_POLICY = "FAILED_POLICY"
    FAILED_ENGINE = "FAILED_ENGINE"
    STREAM_ERROR = "STREAM_ERROR"


class TerminalOutcomeResolver:
    """Resolve competing completion signals with verified flags first."""

    PRIORITY = (
        TerminalOutcome.VERIFIED_FLAG,
        TerminalOutcome.COMPLETED_SOLVED,
        TerminalOutcome.COMPLETED_UNSOLVED,
        TerminalOutcome.CANCELLED,
        TerminalOutcome.FAILED_POLICY,
        TerminalOutcome.FAILED_ENGINE,
        TerminalOutcome.STREAM_ERROR,
    )

    def resolve(
        self,
        status: str | None = None,
        *,
        flag_verified: bool = False,
        completed_solved: bool = False,
        completed_unsolved: bool = False,
        cancelled: bool = False,
        failed_policy: bool = False,
        failed_engine: bool = False,
        stream_error: bool = False,
    ) -> str:
        signals = {
            TerminalOutcome.VERIFIED_FLAG: flag_verified,
            TerminalOutcome.COMPLETED_SOLVED: completed_solved or status == "COMPLETED_SOLVED",
            TerminalOutcome.COMPLETED_UNSOLVED: completed_unsolved or status == "COMPLETED_UNSOLVED",
            TerminalOutcome.CANCELLED: cancelled or status == "CANCELLED",
            TerminalOutcome.FAILED_POLICY: failed_policy or status in {"POLICY_BLOCKED", "FAILED_POLICY"},
            TerminalOutcome.FAILED_ENGINE: failed_engine or status in {"FAILED_ENGINE", "FAILED_TOOL", "FAILED_RUNNER"},
            TerminalOutcome.STREAM_ERROR: stream_error,
        }
        for outcome in self.PRIORITY:
            if signals[outcome]:
                return outcome.value
        return str(status or TerminalOutcome.STREAM_ERROR)


terminal_outcome_resolver = TerminalOutcomeResolver()
