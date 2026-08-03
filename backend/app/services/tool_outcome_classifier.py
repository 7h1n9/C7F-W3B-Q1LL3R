"""Classify Runner results before they reach planning/controller logic.

The Runner can finish a request without producing a fact, reject the result
contract, or fail to execute at all.  Those are different outcomes and must
not be represented by one generic ``FAILED`` value.
"""

from enum import Enum
from typing import Any


class ToolOutcome(str, Enum):
    SUCCESS = "SUCCESS"
    NO_FACT = "NO_FACT"
    LOW_SIGNAL = "LOW_SIGNAL"
    RETRYABLE_ERROR = "RETRYABLE_ERROR"
    CONTRACT_ERROR = "CONTRACT_ERROR"
    TERMINAL_FAILURE = "TERMINAL_FAILURE"
    # Backwards-compatible name used by the first implementation.
    INFRA_ERROR = "TERMINAL_FAILURE"


def _value(result: Any, key: str, default: Any = None) -> Any:
    if isinstance(result, dict):
        return result.get(key, default)
    return getattr(result, key, default)


def classify_tool_outcome(result: Any) -> ToolOutcome:
    """Return the durable semantic outcome of a tool result.

    Ordering is intentional: an explicit contract error wins over generic
    retryability, while ``NO_FACT`` is a completed execution and not an
    infrastructure failure.
    """
    status = str(_value(result, "result_status", _value(result, "status", "")) or "").upper()
    error_code = str(_value(result, "error_code", "") or "").upper()
    stage = str(_value(result, "stage", "") or "").upper()
    error = str(_value(result, "error", _value(result, "error_message", "")) or "").lower()

    if status in {"SUCCESS", "COMPLETED", "CACHED"}:
        return ToolOutcome.SUCCESS
    if status in {"CONTRACT_ERROR", "RESULT_CONTRACT"} or stage == "RESULT_CONTRACT" or error_code.startswith("RESULT_CONTRACT") or error_code.startswith("MYSQL_METADATA_CONTRACT"):
        return ToolOutcome.CONTRACT_ERROR
    if status == "NO_FACT" or error_code in {"MYSQL_METADATA_EMPTY_RESULT", "MYSQL_METADATA_STAGE_EMPTY_RESULT"}:
        return ToolOutcome.NO_FACT
    confidence = _value(result, "confidence", None)
    if status == "LOW_SIGNAL" or (isinstance(confidence, (int, float)) and float(confidence) < 0.8 and _value(result, "signal_features", None) is not None):
        return ToolOutcome.LOW_SIGNAL
    if status in {"TIMEOUT", "CANCELLED"} or _value(result, "retryable", False) or any(token in error for token in ("timeout", "timed out", "network", "connection")) or error_code in {"RUNNER_UNAVAILABLE", "RUNNER_JOB_FAILED", "TOOL_RESULT_DELIVERY_FAILED"}:
        return ToolOutcome.RETRYABLE_ERROR
    # Unknown execution failures are environmental/infrastructure failures
    # until the Runner supplies a more specific contract classification.
    return ToolOutcome.INFRA_ERROR


class ToolOutcomeClassifier:
    """Stateless façade for dependency-injected/controller call sites."""

    @staticmethod
    def classify(result: Any) -> ToolOutcome:
        return classify_tool_outcome(result)


# Short alias keeps call sites readable and supports callers that prefer a
# verb over the longer public name.
classify = classify_tool_outcome
