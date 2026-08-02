"""Compatibility/public name for the execution outcome classifier."""

from app.services.tool_outcome_classifier import (
    ToolOutcome,
    ToolOutcomeClassifier,
    classify,
    classify_tool_outcome,
)

ExecutionOutcome = ToolOutcome
ExecutionOutcomeClassifier = ToolOutcomeClassifier

__all__ = [
    "ExecutionOutcome",
    "ExecutionOutcomeClassifier",
    "ToolOutcome",
    "ToolOutcomeClassifier",
    "classify",
    "classify_tool_outcome",
]
