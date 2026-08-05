"""Durable uniqueness and progression records for multi-agent experiments."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from app.services.solver_state import solver_state_service


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _request_identity(*, stage: str, arguments: dict, independent_variable: str) -> dict:
    """Return the semantic request identity used by experiment uniqueness.

    Keep strategy prose out of this identity: changing a hypothesis must not
    make an identical request executable again.  Conversely, the request
    contract and its stage must be explicit so two bounded experiments that
    differ only in a business input remain distinct.
    """
    body = arguments.get("json") if "json" in arguments else arguments.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except (TypeError, ValueError):
            pass
    endpoint = arguments.get("endpoint") or arguments.get("url") or ""
    modified_fields = arguments.get("modified_fields")
    if modified_fields is None:
        modified_fields = [independent_variable] if independent_variable else []
    elif isinstance(modified_fields, str):
        modified_fields = [modified_fields]
    return {
        "method": str(arguments.get("method") or "").upper(),
        "endpoint": str(endpoint),
        "body": body,
        "modified_fields": sorted({str(item) for item in modified_fields if str(item)}),
        "stage": str(stage or "").upper(),
    }


class ExperimentStrategyManager:
    def fingerprint(self, *, tool_name: str, stage: str, arguments: dict, independent_variable: str, hypothesis: str) -> str:
        payload = {
            "tool_name": tool_name,
            "request": _request_identity(stage=stage, arguments=arguments, independent_variable=independent_variable),
            "hypothesis": hypothesis,
        }
        return hashlib.sha256(_canonical(payload).encode()).hexdigest()

    def record(self, *, tool_name: str, stage: str, arguments: dict, independent_variable: str, hypothesis: str, expected_signal=None) -> dict:
        request_identity = _request_identity(stage=stage, arguments=arguments, independent_variable=independent_variable)
        request_fingerprint = hashlib.sha256(_canonical(request_identity).encode()).hexdigest()
        experiment_id = self.fingerprint(tool_name=tool_name, stage=stage, arguments=arguments, independent_variable=independent_variable, hypothesis=hypothesis)
        return {"experiment_id": experiment_id, "tool_name": tool_name, "stage": stage, "hypothesis": hypothesis, "independent_variable": independent_variable, "changed_fields": request_identity["modified_fields"], "request_identity": request_identity, "request_fingerprint": request_fingerprint, "expected_signal": expected_signal or {}, "observed_signal": {}, "result": "RESERVED", "next_allowed_actions": [], "updated_at": datetime.now(UTC).isoformat()}

    async def reserve(self, session, run, *, tool_name: str, stage: str, arguments: dict, independent_variable: str, hypothesis: str, expected_signal=None) -> tuple[bool, dict]:
        entry = self.record(tool_name=tool_name, stage=stage, arguments=arguments, independent_variable=independent_variable, hypothesis=hypothesis, expected_signal=expected_signal)
        state = await solver_state_service.load(session, run.id)
        history = dict((state.action_fingerprints_json if state else {}) or {})
        previous = history.get(entry["experiment_id"])
        if previous:
            return False, previous
        # A model must not evade the five-call duplicate guard by changing
        # only its prose hypothesis while replaying the same request.
        for prior in history.values():
            if not isinstance(prior, dict):
                continue
            if (
                prior.get("tool_name") == entry["tool_name"]
                and prior.get("stage") == entry["stage"]
                and prior.get("request_fingerprint") == entry["request_fingerprint"]
            ):
                return False, prior
        if state is not None:
            state.action_fingerprints_json = {**history, entry["experiment_id"]: entry}
            ledger = dict(state.capability_ledger_json or {})
            experiments = list(ledger.get("experiment_history") or [])
            state.capability_ledger_json = {**ledger, "experiment_history": [*experiments, entry][-100:]}
            await session.commit()
        return True, entry

    async def record_result(self, session, run, experiment_id: str, *, observed_signal=None, result: str, next_allowed_actions=None) -> None:
        state = await solver_state_service.load(session, run.id)
        if state is None:
            return
        history = dict(state.action_fingerprints_json or {})
        entry = dict(history.get(experiment_id) or {})
        if not entry:
            return
        entry.update({"observed_signal": observed_signal or {}, "result": result, "next_allowed_actions": next_allowed_actions or [], "updated_at": datetime.now(UTC).isoformat()})
        state.action_fingerprints_json = {**history, experiment_id: entry}
        ledger = dict(state.capability_ledger_json or {})
        experiments = [entry if item.get("experiment_id") == experiment_id else item for item in (ledger.get("experiment_history") or [])]
        state.capability_ledger_json = {**ledger, "experiment_history": experiments}
        await session.commit()


experiment_strategy_manager = ExperimentStrategyManager()
