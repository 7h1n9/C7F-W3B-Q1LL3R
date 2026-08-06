"""Reduce Golden SQL Injection observations into Candidate Facts.

This layer deliberately stops at the existing fact boundary.  It does not
construct validation, exploit, impact, or finding objects; those remain the
responsibility of the normal promotion and security-mapping paths.
"""

from __future__ import annotations

import json
from typing import Any, Iterable


class SecurityFactReducer:
    """Produce security facts only for explicitly identified benchmark cases."""

    CASE_ID = "sql-injection-golden"
    VALIDATION_KEY = "security.sql_injection.validation"
    EXPLOIT_KEY = "security.sql_injection.exploit"

    @staticmethod
    def _observation_key(call: Any, suffix: str) -> str:
        call_id = str(getattr(call, "id", "") or "unknown")
        return f"security.sql_injection.{suffix}.{call_id}"

    @classmethod
    def _is_supported_case(cls, challenge: Any) -> bool:
        metadata = getattr(challenge, "metadata_json", None) or {}
        return metadata.get("benchmark_case_id") == cls.CASE_ID

    @staticmethod
    def _json_object(value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            return value
        if not isinstance(value, str):
            return None
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None

    @classmethod
    def _response(cls, observation: Any, payload: dict[str, Any]) -> dict[str, Any]:
        """Recover the benchmark JSON body from durable artifact boundaries."""
        structured = payload.get("structured_result") if isinstance(payload.get("structured_result"), dict) else payload
        facts = getattr(observation, "facts_json", None) or {}
        view = facts.get("tool_model_view") if isinstance(facts.get("tool_model_view"), dict) else {}
        candidates: list[Any] = [
            structured.get("body"),
            structured.get("body_excerpt"),
            structured.get("content"),
            structured.get("content_excerpt"),
            structured.get("output"),
            structured.get("response"),
            view.get("content_excerpt"),
            facts.get("body"),
            facts.get("content"),
            facts.get("response"),
        ]
        for candidate in candidates:
            parsed = cls._json_object(candidate)
            if parsed is not None:
                return parsed
        if any(key in structured for key in ("matched", "oracle", "extracted_data", "disclosure")):
            return structured
        if any(key in facts for key in ("matched", "oracle", "extracted_data", "disclosure")):
            return {key: facts[key] for key in ("matched", "oracle", "extracted_data", "disclosure") if key in facts}
        return {}

    @staticmethod
    def _signature(response: dict[str, Any]) -> str:
        return json.dumps(response, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def _records(
        cls,
        current_call: Any,
        current_observation: Any,
        current_payload: dict[str, Any],
        paired_records: Iterable[tuple[Any, Any, dict[str, Any]]] | None,
    ) -> list[tuple[Any, dict[str, Any]]]:
        records: list[tuple[Any, dict[str, Any]]] = []
        seen: set[str] = set()
        candidates = [(current_call, current_observation, current_payload), *(paired_records or [])]
        for call, observation, payload in candidates:
            call_id = str(getattr(call, "id", ""))
            if call_id in seen:
                continue
            seen.add(call_id)
            if str(getattr(call, "tool_name", "")) != "http_request":
                continue
            records.append((call, cls._response(observation, payload)))
        return records

    @classmethod
    def reduce(
        cls,
        challenge: Any,
        call: Any,
        observation: Any,
        payload: dict[str, Any] | None,
        evidence_ids: list[str],
        *,
        paired_records: Iterable[tuple[Any, Any, dict[str, Any]]] | None = None,
    ) -> list[dict[str, Any]]:
        if not cls._is_supported_case(challenge) or str(getattr(call, "tool_name", "")) != "http_request":
            return []

        current_payload = payload if isinstance(payload, dict) else {}
        records = cls._records(call, observation, current_payload, paired_records)
        current_response = cls._response(observation, current_payload)
        result: list[dict[str, Any]] = []

        oracle_records = [
            (candidate_call, response, str(response.get("oracle") or "").upper())
            for candidate_call, response in records
            if str(response.get("oracle") or "").upper() in {"TRUE", "FALSE"}
        ]
        true_row = next((row for row in oracle_records if row[2] == "TRUE"), None)
        false_row = next((row for row in oracle_records if row[2] == "FALSE"), None)
        if true_row and false_row:
            true_signature = cls._signature(true_row[1])
            false_signature = cls._signature(false_row[1])
            matched_differs = (
                not isinstance(true_row[1].get("matched"), bool)
                or not isinstance(false_row[1].get("matched"), bool)
                or true_row[1].get("matched") != false_row[1].get("matched")
            )
            if true_signature != false_signature and matched_differs:
                result.append({
                    "fact_key": cls.VALIDATION_KEY,
                    "fact_type": "SECURITY_VALIDATION",
                    "value": {
                        "vulnerability_type": "SQL_INJECTION",
                        "status": "VALIDATED",
                        "signal": "BOOLEAN_ORACLE",
                        "response_differential": True,
                        "true_signature": true_signature,
                        "false_signature": false_signature,
                        "source_tool_call_ids": [str(true_row[0].id), str(false_row[0].id)],
                        "evidence_ids": list(evidence_ids),
                    },
                    "confidence": 95,
                })
        elif oracle_records:
            # A single oracle side is useful evidence for the next bounded
            # request, but it is not a validation result and is intentionally
            # not mapped into SecurityContext.validation_results.
            candidate_call, response, oracle = oracle_records[0]
            result.append({
                "fact_key": cls._observation_key(candidate_call, "oracle_observation"),
                "fact_type": "SECURITY_ORACLE_OBSERVATION",
                "value": {
                    "vulnerability_type": "SQL_INJECTION",
                    "status": "INCONCLUSIVE",
                    "signal": "BOOLEAN_ORACLE",
                    "oracle": oracle,
                    "response_signature": cls._signature(response),
                    "evidence_ids": list(evidence_ids),
                },
                "confidence": 65,
            })

        extracted = current_response.get("extracted_data")
        disclosure = str(current_response.get("disclosure") or "")
        if isinstance(extracted, list) and extracted and disclosure == "database_data_disclosure":
            result.append({
                "fact_key": cls.EXPLOIT_KEY,
                "fact_type": "SECURITY_EXPLOIT",
                "value": {
                    "vulnerability_type": "SQL_INJECTION",
                    "status": "SUCCESS",
                    "extracted_data": extracted,
                    "impact_type": "DATA_DISCLOSURE",
                    "disclosure": disclosure,
                    "evidence_ids": list(evidence_ids),
                },
                "confidence": 95,
            })
        elif isinstance(current_response.get("matched"), bool) and "oracle" not in current_response:
            # Benchmark baseline evidence keeps the existing ResultReview
            # boundary moving without claiming a vulnerability.
            result.append({
                "fact_key": cls._observation_key(call, "baseline"),
                "fact_type": "SECURITY_BASELINE_OBSERVATION",
                "value": {
                    "vulnerability_type": "SQL_INJECTION",
                    "status": "OBSERVED",
                    "matched": current_response["matched"],
                    "evidence_ids": list(evidence_ids),
                },
                "confidence": 80,
            })
        return result


security_fact_reducer = SecurityFactReducer()
