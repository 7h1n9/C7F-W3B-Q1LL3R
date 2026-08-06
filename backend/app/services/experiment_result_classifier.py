"""Classify experiment outcomes and translate them into strategy feedback.

This layer is deliberately downstream of execution and evidence production.  It
does not promote facts and it does not dispatch tools; it only turns a durable
experiment result into a bounded migration recommendation for the Planner.
"""

from __future__ import annotations

from typing import Any, Mapping


class StrategyMigrationEngine:
    """Choose the next strategy family without retrying the failed one."""

    FAMILY_LIMITS = {"BOOLEAN": 3}

    def recommend(
        self,
        *,
        vulnerability_type: str = "",
        strategy_family: str,
        strategy_variant: str,
        classification: str,
        family_attempts: int = 0,
    ) -> dict[str, Any]:
        family = str(strategy_family or "GENERAL").upper()
        variant = str(strategy_variant or "").upper()
        reason = "The current strategy remains available."
        recommendations: list[str] = []
        exhausted = family_attempts >= self.FAMILY_LIMITS.get(family, 10**9)

        if classification == "ORACLE_CONFIRMED":
            reason = "The strategy produced a stable, differential oracle and must not be repeated."
        elif family == "BOOLEAN" and exhausted:
            recommendations = ["UNION", "TIME_BASED"]
            reason = "The BOOLEAN strategy family exhausted its bounded budget; migrate to independent signal families."
        elif classification == "TRUE_SIDE_FAILED":
            recommendations = ["OR"] if variant != "OR" else ["ERROR_BASED"]
            reason = "The TRUE side failed while the FALSE control was stable; migrate the predicate family."
        elif classification == "NO_DIFFERENCE":
            recommendations = ["ERROR_BASED"]
            reason = "Both controls were stable but the response signal did not differ; change the signal strategy."
        elif classification == "FALSE_SIDE_FAILED":
            recommendations = ["VALIDATE_NEGATIVE_CONTROL"]
            reason = "The negative control is unstable; validate the control before reusing the payload family."
        elif classification == "NO_SIGNAL":
            recommendations = ["OR"] if variant != "OR" else ["ERROR_BASED"]
            reason = "The current payload family did not form a stable signal."
        elif classification == "FAILED":
            recommendations = ["RETRY_ONCE"]
            reason = "The execution failed before producing a semantic validation result; one bounded recovery is allowed."

        source = "/".join(
            part for part in (
                str(vulnerability_type or "").upper(),
                str(strategy_family or "GENERAL").upper(),
                str(strategy_variant or "").upper(),
            ) if part
        )
        destination: str | list[str] = recommendations[0] if len(recommendations) == 1 else list(recommendations)
        return {
            "family_exhausted": exhausted,
            "recommended_strategies": recommendations,
            "reason": reason,
            "current_strategy": {"family": family, "variant": variant},
            "from": source,
            "to": destination,
            "previous_attempts": family_attempts,
        }

    # A small alias makes the engine convenient for callers that treat the
    # migration component as a pure recommendation service.
    suggest = recommend


class ExperimentResultClassifier:
    """Normalize raw tool/diagnosis output into a bounded experiment result."""

    DIAGNOSIS_CLASSES = {
        "TRUE_SIDE_FAILED",
        "FALSE_SIDE_FAILED",
        "NO_DIFFERENCE",
        "NO_SIGNAL",
        "ORACLE_CONFIRMED",
    }

    def __init__(self, migration_engine: StrategyMigrationEngine | None = None) -> None:
        self.migration_engine = migration_engine or StrategyMigrationEngine()

    @staticmethod
    def _mapping(value: Any) -> Mapping[str, Any]:
        return value if isinstance(value, Mapping) else {}

    def classify(
        self,
        result: Mapping[str, Any] | None = None,
        *,
        diagnosis: Mapping[str, Any] | None = None,
        strategy: Mapping[str, Any] | None = None,
        family_attempts: int = 0,
        explicit_result: str | None = None,
    ) -> dict[str, Any]:
        raw = self._mapping(result)
        diagnostic = self._mapping(diagnosis) or raw
        classification = str(diagnostic.get("classification") or "").upper()
        explicit = str(explicit_result or raw.get("result") or raw.get("status") or "").upper()
        if not classification and any(
            key in diagnostic
            for key in ("stable_true", "stable_false", "response_differential", "true_false_differential", "boolean_oracle_confirmed")
        ):
            stable_true = diagnostic.get("stable_true") is True
            stable_false = diagnostic.get("stable_false") is True
            differential = diagnostic.get("response_differential") is True or diagnostic.get("true_false_differential") is True
            confirmed = diagnostic.get("boolean_oracle_confirmed") is True
            if confirmed and stable_true and stable_false and differential:
                classification = "ORACLE_CONFIRMED"
            elif not stable_true and stable_false:
                classification = "TRUE_SIDE_FAILED"
            elif stable_true and not stable_false:
                classification = "FALSE_SIDE_FAILED"
            elif stable_true and stable_false and not differential:
                classification = "NO_DIFFERENCE"
            else:
                classification = "NO_SIGNAL"
        if classification not in self.DIAGNOSIS_CLASSES:
            classification = ""

        if classification == "ORACLE_CONFIRMED" or explicit == "CONFIRMED":
            status = "CONFIRMED"
            normalized_classification = classification or "CONFIRMED"
            reason = "The experiment produced a confirmed semantic result."
        elif classification in self.DIAGNOSIS_CLASSES:
            status = "INCONCLUSIVE"
            normalized_classification = classification
            reason = str(diagnostic.get("reason") or "The experiment did not produce a confirmed result.")
        elif explicit in {"FAILED", "ERROR", "TIMEOUT"}:
            status = "FAILED"
            normalized_classification = "FAILED"
            reason = str(raw.get("failure_reason") or raw.get("error") or "The experiment execution failed.")
        elif explicit in {"INCONCLUSIVE", "PARTIAL"}:
            status = "INCONCLUSIVE"
            normalized_classification = explicit
            reason = str(raw.get("failure_reason") or "The experiment did not produce sufficient evidence.")
        elif explicit in {"COMPLETED", "SUCCESS", "VALIDATED"}:
            status = "COMPLETED"
            normalized_classification = explicit
            reason = "The experiment completed without a semantic confirmation classification."
        else:
            status = "FAILED"
            normalized_classification = "FAILED"
            reason = "The experiment result was not interpretable."

        current_strategy = self._mapping(strategy)
        migration = self.migration_engine.recommend(
            vulnerability_type=str(current_strategy.get("vulnerability_type") or current_strategy.get("type") or ""),
            strategy_family=str(current_strategy.get("strategy_family") or current_strategy.get("family") or "GENERAL"),
            strategy_variant=str(current_strategy.get("strategy_variant") or current_strategy.get("variant") or ""),
            classification=normalized_classification,
            family_attempts=family_attempts,
        )
        if status == "CONFIRMED":
            migration["recommended_strategies"] = []
            migration["family_exhausted"] = False

        next_action = str(diagnostic.get("next_action") or "")
        return {
            "status": status,
            "result": status,
            "classification": normalized_classification,
            "confidence": float(diagnostic.get("confidence") or (0.95 if status == "CONFIRMED" else 0.75)),
            "reason": reason,
            "next_action": next_action,
            "recommended_strategies": list(migration.get("recommended_strategies") or []),
            "strategy_migration": migration,
            "failure_reason": raw.get("failure_reason") or (normalized_classification if status == "INCONCLUSIVE" else None),
        }


experiment_result_classifier = ExperimentResultClassifier()
strategy_migration_engine = experiment_result_classifier.migration_engine
