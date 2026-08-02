"""Bounded progress counters used only by RunSupervisor."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ProgressDecision:
    new_fact_or_capability: bool = False
    retryable: bool = False
    needs_user: bool = False
    terminal_unsolved: bool = False
    reason: str = ""


class SupervisorProgressEvaluator:
    def observe(self, checkpoint: dict, *, stage: str, error_code: str | None,
                before_facts: set[str], after_facts: set[str],
                before_capabilities: set[str], after_capabilities: set[str],
                candidate_exists: bool = False) -> ProgressDecision:
        counters = dict(checkpoint.get("supervisor_counters") or {})
        counters["stage_attempt_count"] = int(counters.get("stage_attempt_count", 0)) + 1
        counters["no_progress_count"] = int(counters.get("no_progress_count", 0)) + 1
        counters["same_error_count"] = int(counters.get("same_error_count", 0)) + 1 if error_code else 0
        counters["last_key"] = f"{stage}:{error_code or 'NONE'}"
        new_progress = bool((after_facts - before_facts) or (after_capabilities - before_capabilities) or candidate_exists)
        if new_progress:
            counters["no_progress_count"] = 0
            counters["same_error_count"] = 0
        checkpoint["supervisor_counters"] = counters
        if error_code == "SERVICE_RESTART_INTERRUPTED_TASK" and counters["same_error_count"] >= 2:
            return ProgressDecision(needs_user=True, reason="The interrupted task could not be recovered twice.")
        if error_code in {"MYSQL_METADATA_EMPTY_RESULT", "MYSQL_METADATA_STAGE_EMPTY_RESULT"} and counters["same_error_count"] >= 2:
            return ProgressDecision(needs_user=True, reason="Metadata discovery repeatedly returned no required facts.")
        if error_code == "MYSQL_PREDICATE_NOT_CONFIRMED":
            return ProgressDecision(terminal_unsolved=True, reason=error_code)
        if counters["no_progress_count"] >= 2:
            return ProgressDecision(terminal_unsolved=True, reason="NO_PROGRESS_LOOP")
        return ProgressDecision(new_fact_or_capability=new_progress, retryable=True)


supervisor_progress_evaluator = SupervisorProgressEvaluator()
