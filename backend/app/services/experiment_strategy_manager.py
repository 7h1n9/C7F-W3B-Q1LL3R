"""Durable two-layer identity and progression records for experiments."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime

from app.services.action_fingerprint import (
    build_execution_fingerprint,
    build_strategy_fingerprint,
    canonical_json,
    normalize_request,
)
from app.services.solver_state import solver_state_service


MAX_EXACT_RETRIES = 1
STRATEGY_FAMILY_LIMITS = {
    ("SQL_INJECTION", "BOOLEAN"): 3,
}


def _canonical(value) -> str:
    return canonical_json(value)


def _status(entry: dict | None) -> str:
    return str((entry or {}).get("status") or (entry or {}).get("result") or "RESERVED").upper()


def _request_identity(*, stage: str, arguments: dict, independent_variable: str) -> dict:
    normalized = normalize_request(arguments)
    modified_fields = arguments.get("modified_fields")
    if modified_fields is None:
        modified_fields = arguments.get("test_field") or independent_variable
    if isinstance(modified_fields, str):
        modified_fields = [modified_fields]
    body = normalized.get("json") if normalized.get("json") is not None else normalized.get("body")
    return {
        "method": normalized["method"],
        "endpoint": normalized["endpoint"],
        "body": body,
        "modified_fields": sorted({str(item) for item in (modified_fields or []) if str(item)}),
        "stage": str(stage or "").upper(),
    }


def _metadata_from_arguments(
    *,
    tool_name: str,
    stage: str,
    arguments: dict,
    independent_variable: str,
) -> dict:
    args = dict(arguments or {})
    if str(tool_name) == "sql_boolean_compare":
        true_condition = str(args.get("true_condition") or "").lower()
        false_condition = str(args.get("false_condition") or "").lower()
        combined = f"{true_condition} {false_condition}"
        if "sleep(" in combined or "benchmark(" in combined:
            family, variant, signal = "TIME_BASED", "DELAY", "TIMING_DIFFERENTIAL"
        elif "union" in combined:
            family, variant, signal = "UNION", "SELECT", "CONTENT_DIFFERENTIAL"
        elif any(item in combined for item in ("extractvalue", "updatexml", "floor(")):
            family, variant, signal = "ERROR_BASED", "ERROR", "ERROR_DIFFERENTIAL"
        elif re.search(r"\bor\b", combined):
            family, variant, signal = "BOOLEAN", "OR", "RESPONSE_DIFFERENTIAL"
        elif "#" in combined:
            family, variant, signal = "BOOLEAN", "AND_COMMENT_HASH", "RESPONSE_DIFFERENTIAL"
        elif "/**/" in combined:
            family, variant, signal = "BOOLEAN", "AND_COMMENT_INLINE", "RESPONSE_DIFFERENTIAL"
        elif "%27" in combined or "%20" in combined:
            family, variant, signal = "BOOLEAN", "AND_ENCODING", "RESPONSE_DIFFERENTIAL"
        else:
            family, variant, signal = "BOOLEAN", "AND", "RESPONSE_DIFFERENTIAL"
        return {
            "vulnerability_type": "SQL_INJECTION",
            "target": {
                "endpoint": normalize_request(args).get("endpoint"),
                "parameter": args.get("test_field") or independent_variable,
            },
            "strategy_family": family,
            "strategy_variant": variant,
            "signal_type": signal,
            "encoding": str(args.get("encoding") or "PLAIN"),
        }
    if str(tool_name) == "http_request" and str(stage).upper() == "BUSINESS_BASELINE":
        return {
            "vulnerability_type": "BUSINESS_BASELINE",
            "target": {"endpoint": normalize_request(args).get("endpoint"), "parameter": independent_variable},
            "strategy_family": "BASELINE",
            "strategy_variant": str(stage or "BASELINE"),
            "signal_type": "BUSINESS_RESPONSE",
            "encoding": "PLAIN",
        }
    return {
        "vulnerability_type": "GENERAL",
        "target": {"endpoint": normalize_request(args).get("endpoint"), "parameter": independent_variable},
        "strategy_family": str(stage or "GENERAL").upper(),
        "strategy_variant": str(tool_name or "GENERAL").upper(),
        "signal_type": "OBSERVATION",
        "encoding": "PLAIN",
    }


class ExperimentStrategyManager:
    def _strategy_metadata(
        self,
        *,
        tool_name: str,
        stage: str,
        arguments: dict,
        independent_variable: str,
        strategy_metadata: dict | None,
    ) -> dict:
        inferred = _metadata_from_arguments(
            tool_name=tool_name,
            stage=stage,
            arguments=arguments,
            independent_variable=independent_variable,
        )
        supplied = dict(strategy_metadata or {})
        target = {**(inferred.get("target") or {}), **(supplied.get("target") or {})}
        return {
            **inferred,
            **supplied,
            "target": target,
            "vulnerability_type": str(supplied.get("vulnerability_type") or inferred["vulnerability_type"]).upper(),
            "strategy_family": str(supplied.get("strategy_family") or inferred["strategy_family"]).upper(),
            "strategy_variant": str(supplied.get("strategy_variant") or inferred["strategy_variant"]).upper(),
            "signal_type": str(supplied.get("signal_type") or inferred["signal_type"]).upper(),
            "encoding": str(supplied.get("encoding") or inferred["encoding"]).upper(),
            "payload_family": str(supplied.get("payload_family") or inferred.get("payload_family") or "").upper(),
        }

    def fingerprint(
        self,
        *,
        tool_name: str,
        stage: str,
        arguments: dict,
        independent_variable: str,
        hypothesis: str,
        strategy_metadata: dict | None = None,
    ) -> str:
        metadata = self._strategy_metadata(
            tool_name=tool_name,
            stage=stage,
            arguments=arguments,
            independent_variable=independent_variable,
            strategy_metadata=strategy_metadata,
        )
        execution = build_execution_fingerprint(tool_name, arguments, stage=stage)
        strategy = build_strategy_fingerprint(
            vulnerability_type=metadata["vulnerability_type"],
            target=metadata["target"],
            strategy_family=metadata["strategy_family"],
            strategy_variant=metadata["strategy_variant"],
            signal_type=metadata["signal_type"],
            encoding=metadata["encoding"],
            payload_family=metadata["payload_family"],
        )
        return hashlib.sha256(_canonical({"execution": execution, "strategy": strategy}).encode()).hexdigest()

    def record(
        self,
        *,
        tool_name: str,
        stage: str,
        arguments: dict,
        independent_variable: str,
        hypothesis: str,
        expected_signal=None,
        strategy_metadata: dict | None = None,
    ) -> dict:
        request_identity = _request_identity(stage=stage, arguments=arguments, independent_variable=independent_variable)
        metadata = self._strategy_metadata(
            tool_name=tool_name,
            stage=stage,
            arguments=arguments,
            independent_variable=independent_variable,
            strategy_metadata=strategy_metadata,
        )
        execution_fingerprint = build_execution_fingerprint(tool_name, arguments, stage=stage)
        strategy_fingerprint = build_strategy_fingerprint(
            vulnerability_type=metadata["vulnerability_type"],
            target=metadata["target"],
            strategy_family=metadata["strategy_family"],
            strategy_variant=metadata["strategy_variant"],
            signal_type=metadata["signal_type"],
            encoding=metadata["encoding"],
            payload_family=metadata["payload_family"],
        )
        experiment_id = hashlib.sha256(_canonical({"execution": execution_fingerprint, "strategy": strategy_fingerprint}).encode()).hexdigest()
        return {
            "experiment_id": experiment_id,
            "execution_fingerprint": execution_fingerprint,
            "strategy_fingerprint": strategy_fingerprint,
            "request_fingerprint": execution_fingerprint,
            "tool_name": tool_name,
            "stage": stage,
            "hypothesis": hypothesis,
            "independent_variable": independent_variable,
            "changed_fields": request_identity["modified_fields"],
            "request_identity": request_identity,
            "expected_signal": expected_signal or {},
            "observed_signal": {},
            "vulnerability_type": metadata["vulnerability_type"],
            "target": metadata["target"],
            "strategy_family": metadata["strategy_family"],
            "strategy_variant": metadata["strategy_variant"],
            "signal_type": metadata["signal_type"],
            "encoding": metadata["encoding"],
            "payload_family": metadata["payload_family"],
            "status": "RESERVED",
            "result": "RESERVED",
            "attempt_count": 1,
            "failure_reason": None,
            "next_allowed_actions": [],
            "updated_at": datetime.now(UTC).isoformat(),
        }

    @staticmethod
    def _family_limit(entry: dict) -> int | None:
        return STRATEGY_FAMILY_LIMITS.get(
            (str(entry.get("vulnerability_type") or "").upper(), str(entry.get("strategy_family") or "").upper())
        )

    @staticmethod
    def _same_strategy(entry: dict, candidate: dict) -> bool:
        return bool(entry.get("strategy_fingerprint")) and entry.get("strategy_fingerprint") == candidate.get("strategy_fingerprint")

    @staticmethod
    def _replace_history(history_list: list, entry: dict) -> list:
        replaced = False
        result = []
        for item in history_list:
            if isinstance(item, dict) and item.get("experiment_id") == entry.get("experiment_id"):
                result.append(entry)
                replaced = True
            else:
                result.append(item)
        return [*result, entry] if not replaced else result

    async def _persist(self, session, state, history: dict, entry: dict) -> None:
        if state is None:
            return
        state.action_fingerprints_json = {**history, entry["experiment_id"]: entry}
        ledger = dict(state.capability_ledger_json or {})
        experiments = self._replace_history(list(ledger.get("experiment_history") or []), entry)
        state.capability_ledger_json = {**ledger, "experiment_history": experiments[-100:]}
        await session.commit()

    async def reserve(
        self,
        session,
        run,
        *,
        tool_name: str,
        stage: str,
        arguments: dict,
        independent_variable: str,
        hypothesis: str,
        expected_signal=None,
        strategy_metadata: dict | None = None,
    ) -> tuple[bool, dict]:
        entry = self.record(
            tool_name=tool_name,
            stage=stage,
            arguments=arguments,
            independent_variable=independent_variable,
            hypothesis=hypothesis,
            expected_signal=expected_signal,
            strategy_metadata=strategy_metadata,
        )
        state = await solver_state_service.load(session, run.id)
        history = dict((state.action_fingerprints_json if state else {}) or {})

        previous = next((item for item in history.values() if isinstance(item, dict) and item.get("execution_fingerprint") == entry["execution_fingerprint"]), None)
        if previous is not None:
            previous_status = _status(previous)
            if previous_status == "FAILED" and int(previous.get("attempt_count") or 1) <= MAX_EXACT_RETRIES:
                retry = {
                    **previous,
                    "status": "RESERVED",
                    "result": "RESERVED",
                    "attempt_count": int(previous.get("attempt_count") or 1) + 1,
                    "failure_reason": None,
                    "retry_of": previous.get("experiment_id"),
                    "updated_at": datetime.now(UTC).isoformat(),
                }
                await self._persist(session, state, history, retry)
                return True, retry
            return False, previous

        confirmed = next(
            (
                item for item in history.values()
                if isinstance(item, dict)
                and self._same_strategy(item, entry)
                and _status(item) == "CONFIRMED"
            ),
            None,
        )
        if confirmed is not None:
            return False, confirmed

        limit = self._family_limit(entry)
        if limit is not None:
            family_attempts = [
                item for item in history.values()
                if isinstance(item, dict)
                and str(item.get("vulnerability_type") or "").upper() == entry["vulnerability_type"]
                and str(item.get("strategy_family") or "").upper() == entry["strategy_family"]
            ]
            if len(family_attempts) >= limit:
                return False, {
                    **entry,
                    "rejection_reason": "STRATEGY_FAMILY_BUDGET_EXHAUSTED",
                    "attempt_count": len(family_attempts),
                }

        await self._persist(session, state, history, entry)
        return True, entry

    async def record_result(
        self,
        session,
        run,
        experiment_id: str,
        *,
        observed_signal=None,
        result: str,
        next_allowed_actions=None,
        failure_reason: str | None = None,
        diagnosis=None,
    ) -> dict:
        state = await solver_state_service.load(session, run.id)
        if state is None:
            return {}
        history = dict(state.action_fingerprints_json or {})
        entry = dict(history.get(experiment_id) or {})
        if not entry:
            return {}

        from app.services.experiment_result_classifier import experiment_result_classifier

        raw_observed = dict(observed_signal or {}) if isinstance(observed_signal, dict) else {}
        raw_diagnosis = dict(diagnosis or {}) if isinstance(diagnosis, dict) else {}
        if not raw_diagnosis and any(key in raw_observed for key in ("stable_true", "stable_false", "response_differential", "boolean_oracle_confirmed", "classification")):
            raw_diagnosis = raw_observed

        family = str(entry.get("strategy_family") or "GENERAL").upper()
        family_attempts = 0
        for item in state.attack_strategy_history_json or []:
            if not isinstance(item, dict):
                continue
            # boolean_oracle_diagnosis keeps a legacy payload-only history
            # entry.  The feedback loop counts only manager-owned experiment
            # identities, otherwise one result would consume the family
            # budget twice.
            if not item.get("experiment_id"):
                continue
            item_family = str(item.get("strategy_family") or item.get("family") or "").upper()
            if not item_family:
                payload_family_name = str(item.get("payload_family") or "").lower()
                item_family = "BOOLEAN" if "boolean" in payload_family_name else payload_family_name.upper()
            if item_family == family and str(item.get("vulnerability_type") or "").upper() == str(entry.get("vulnerability_type") or "").upper():
                family_attempts += 1
        # Include the current result in the bounded family budget.
        family_attempts += 1
        classified = experiment_result_classifier.classify(
            raw_observed,
            diagnosis=raw_diagnosis,
            strategy=entry,
            family_attempts=family_attempts,
            explicit_result=result,
        )
        normalized = classified["status"]
        actions = list(next_allowed_actions or [])
        if not actions:
            actions = list(classified.get("recommended_strategies") or [])
        entry.update({
            "status": normalized,
            "result": normalized,
            "last_result": classified["classification"],
            "observed_signal": observed_signal or {},
            "result_classification": classified["classification"],
            "result_confidence": classified["confidence"],
            "result_reason": classified["reason"],
            "failure_reason": failure_reason or classified.get("failure_reason"),
            "next_allowed_actions": actions,
            "strategy_migration": classified["strategy_migration"],
            "family_attempts": family_attempts,
            "updated_at": datetime.now(UTC).isoformat(),
        })
        attack_history = list(state.attack_strategy_history_json or [])
        replaced = False
        for index, item in enumerate(attack_history):
            if isinstance(item, dict) and item.get("experiment_id") == experiment_id:
                attack_history[index] = dict(entry)
                replaced = True
                break
        if not replaced:
            attack_history.append(dict(entry))
        state.attack_strategy_history_json = attack_history[-200:]
        state.last_experiment_json = dict(entry)
        state.last_result_classification = classified["classification"]
        await self._persist(session, state, history, entry)
        return entry


experiment_strategy_manager = ExperimentStrategyManager()
