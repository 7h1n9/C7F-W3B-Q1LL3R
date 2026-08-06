"""Persistent strategy portfolio for bounded Planner continuation.

This layer owns only the *search budget* after an experiment result.  It does
not create payloads, execute tools, or replace the existing experiment
identity manager.  The portfolio is stored inside the existing SolverState
JSON documents so old databases remain compatible.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Iterable, Mapping

from app.services.solver_state import solver_state_service


DEFAULT_STRATEGIES = (
    "BOOLEAN_AND_COMMENT_HASH",
    "BOOLEAN_AND_COMMENT_INLINE",
    "BOOLEAN_AND_ENCODING",
    "BOOLEAN_OR",
    "ERROR_BASED",
    "UNION",
    "TIME_BASED",
)


def _text(value: Any) -> str:
    return str(value or "").strip().upper().replace("-", "_")


def normalize_strategy(value: Any) -> str:
    """Normalize short migration labels and persisted strategy identities."""
    token = _text(value)
    if not token:
        return ""
    if token in {"OR", "BOOLEAN_OR"}:
        return "BOOLEAN_OR"
    if token in {"AND", "BOOLEAN_AND"}:
        return "BOOLEAN_AND"
    if token in {"TIME", "TIME_BASED"}:
        return "TIME_BASED"
    if token in {"ERROR", "ERROR_BASED"}:
        return "ERROR_BASED"
    if token in {"UNION", "UNION_BASED"}:
        return "UNION"
    return token


def strategy_identity(entry: Mapping[str, Any] | None) -> str:
    entry = entry or {}
    family = _text(entry.get("strategy_family") or entry.get("family"))
    variant = _text(entry.get("strategy_variant") or entry.get("variant"))
    if family in {"UNION", "TIME_BASED", "ERROR_BASED"}:
        return family
    if family == "BOOLEAN":
        return normalize_strategy(f"BOOLEAN_{variant}" if variant else family)
    if family and variant:
        return f"{family}_{variant}"
    return normalize_strategy(variant or family)


def _entries(history: Iterable[Any]) -> list[dict[str, Any]]:
    return [dict(item) for item in history if isinstance(item, Mapping)]


def build_strategy_portfolio(
    history: Iterable[Any] | None,
    latest_experiment: Mapping[str, Any] | None,
    *,
    max_attempts: int = 8,
) -> dict[str, Any]:
    """Build a durable, deterministic portfolio from experiment history."""
    entries = _entries(history or [])
    latest = dict(latest_experiment or {})
    # Baseline requests are execution prerequisites, not attempts against the
    # current vulnerability hypothesis.  Counting them here consumes the
    # strategy budget and makes the portfolio claim that a SQL strategy was
    # tried when only business-state controls were executed.
    typed = [
        item
        for item in entries
        if item.get("experiment_id")
        and (
            _text(item.get("vulnerability_type")) == "SQL_INJECTION"
            or _text(item.get("strategy_family")) in {
                "BOOLEAN",
                "ERROR_BASED",
                "UNION",
                "TIME_BASED",
            }
        )
    ]
    tried: list[dict[str, Any]] = []
    tried_keys: set[str] = set()
    for item in typed:
        identity = strategy_identity(item)
        if not identity:
            continue
        tried_keys.add(identity)
        tried.append({
            "strategy": identity,
            "experiment_id": item.get("experiment_id"),
            "status": item.get("status") or item.get("result"),
            "result": item.get("result_classification") or item.get("last_result") or item.get("result"),
            "failure_reason": item.get("failure_reason") or item.get("result_reason"),
        })

    migration = latest.get("strategy_migration") if isinstance(latest.get("strategy_migration"), Mapping) else {}
    recommended = [normalize_strategy(item) for item in (migration.get("recommended_strategies") or [])]
    ordered = [item for item in [*recommended, *DEFAULT_STRATEGIES] if item]
    remaining = list(dict.fromkeys(item for item in ordered if item not in tried_keys))
    failures = [
        item for item in tried
        if _text(item.get("status")) in {"FAILED", "INCONCLUSIVE", "FAILURE"}
        or _text(item.get("result")) not in {"CONFIRMED", "COMPLETED", "SUCCESS"}
    ]
    attempts = len(typed)
    exhausted = attempts >= max_attempts or not remaining
    current_strategy = strategy_identity(latest) or (tried[-1]["strategy"] if tried else "")
    return {
        "vulnerability_type": _text(latest.get("vulnerability_type") or "SQL_INJECTION"),
        "hypothesis": latest.get("hypothesis") or latest.get("result_reason") or "",
        "tried_strategies": tried,
        "failed_strategies": failures,
        "remaining_strategies": remaining,
        "next_candidates": remaining[:4],
        "current_strategy": current_strategy,
        "attempts": attempts,
        "max_attempts": max_attempts,
        "search_exhausted": exhausted,
        "updated_at": datetime.now(UTC).isoformat(),
    }


class StrategyContinuationService:
    async def update(self, session, run_id: str, *, max_attempts: int = 8) -> dict[str, Any]:
        state = await solver_state_service.load(session, run_id)
        if state is None:
            return {}
        portfolio = build_strategy_portfolio(
            state.attack_strategy_history_json or [],
            state.last_experiment_json or {},
            max_attempts=max_attempts,
        )
        ledger = dict(state.capability_ledger_json or {})
        state.capability_ledger_json = {**ledger, "strategy_portfolio": portfolio}
        state.last_experiment_json = {
            **dict(state.last_experiment_json or {}),
            "strategy_portfolio": portfolio,
        }
        plan = dict(state.run_plan_json or {})
        state.run_plan_json = {**plan, "strategy_portfolio": portfolio}
        await session.flush()
        return portfolio


strategy_continuation_service = StrategyContinuationService()
