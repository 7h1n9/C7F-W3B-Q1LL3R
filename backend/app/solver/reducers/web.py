from __future__ import annotations

import re
from typing import Any

from ..observation import SolverObservation
from .base import KnowledgeUpdate


def _result_views(raw_result: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    structured = raw_result.get("structured_result")
    if isinstance(structured, dict):
        return raw_result, structured
    return (raw_result,)


def _status_code(raw_result: dict[str, Any]) -> int | None:
    for view in _result_views(raw_result):
        for key in ("status_code", "status", "http_status"):
            value = view.get(key)
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                continue
    return None


def _boolean_value(raw_result: dict[str, Any], *keys: str) -> bool | None:
    for view in _result_views(raw_result):
        for key in keys:
            if key not in view:
                continue
            value = view[key]
            # ``stable_true``/``stable_false`` describe repeatability of a
            # side, not the Boolean oracle value itself.  Do not mistake a
            # stable FALSE-side for an oracle TRUE result.
            if key.startswith("stable_"):
                continue
            if isinstance(value, dict):
                value = value.get("value", value.get("matched"))
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
                return value.strip().lower() == "true"
        side = "true" if any(key.startswith("true") for key in keys) else "false"
        rows = view.get(f"{side}_results")
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            oracle_value = rows[0].get("oracle_value")
            if isinstance(oracle_value, bool):
                return oracle_value
    return None


def _stable_boolean_fallback(raw_result: dict[str, Any], key: str) -> bool | None:
    """Read legacy stability flags only when no direct oracle value exists."""
    for view in _result_views(raw_result):
        value = view.get(key)
        if isinstance(value, bool):
            return value
    return None


class WebObservationReducer:
    """Reduce HTTP and Boolean observations to verified Solver knowledge."""

    def reduce(self, observation: SolverObservation) -> KnowledgeUpdate:
        status = str(observation.raw_result.get("status") or "").upper()
        if status == "APPROVAL_REQUIRED":
            return KnowledgeUpdate(
                hypotheses=[
                    {
                        "type": "ACTION_APPROVAL_REQUIRED",
                        "action": observation.action_name,
                        "reason": observation.raw_result.get("reason"),
                    }
                ],
                control_updates={
                    "last_action_authorization": "REQUIRE_APPROVAL",
                    "last_action": observation.action_name,
                },
            )
        if observation.action_name == "http_request":
            return self._reduce_http(observation)
        if observation.action_name == "sql_boolean_compare":
            return self._reduce_boolean(observation)
        if observation.action_name == "mysql_metadata_discovery":
            return self._reduce_metadata(observation)
        if observation.action_name == "sqlite_metadata_discovery":
            return self._reduce_sqlite_metadata(observation)
        if observation.action_name == "oracle_expression_calibration":
            return self._reduce_calibration(observation)
        if observation.action_name == "sql_extract":
            return self._reduce_extraction(observation)
        if observation.action_name == "request_capture":
            return self._reduce_request_capture(observation)
        if observation.action_name == "sqlmap_detect":
            return self._reduce_sqlmap_detect(observation)
        if observation.action_name == "sqlmap_run":
            return self._reduce_sqlmap_run(observation)
        if observation.action_name == "script_run":
            return self._reduce_script_run(observation)
        return KnowledgeUpdate(
            verified_facts=list(observation.facts),
            control_updates={"last_action": observation.action_name},
        )

    @staticmethod
    def _reduce_http(observation: SolverObservation) -> KnowledgeUpdate:
        status = _status_code(observation.raw_result)
        if not observation.success:
            return KnowledgeUpdate(
                hypotheses=[
                    {
                        "type": "HTTP_BASELINE_INCONCLUSIVE",
                        "action": observation.action_name,
                        "reason": "worker did not report success",
                    }
                ],
                control_updates={"baseline_status": "INCONCLUSIVE"},
            )
        facts = [
            {"type": "HTTP_RESPONSE", "status": status, "verified": True},
            {"type": "HTTP_ENDPOINT_FOUND", "status": status, "verified": True},
        ]
        surface = WebObservationReducer._surface_from_http(observation.raw_result)
        if surface:
            facts.append({"type": "HTTP_SURFACE_DISCOVERED", **surface, "verified": True})
        return KnowledgeUpdate(
            verified_facts=facts,
            next_phase="VALIDATION",
            control_updates={"baseline_status": "BASELINE_CONFIRMED"},
        )

    @staticmethod
    def _surface_from_http(raw_result: dict[str, Any]) -> dict[str, Any]:
        extracted = raw_result.get("extracted_facts")
        if not isinstance(extracted, dict):
            model_view = raw_result.get("model_view")
            extracted = model_view.get("extracted_facts") if isinstance(model_view, dict) else {}
        body = str(raw_result.get("body_excerpt") or "")
        if not body and isinstance(raw_result.get("model_view"), dict):
            body = str(raw_result["model_view"].get("content_excerpt") or "")
        endpoint_match = re.search(r"/(?:api|v1)/[A-Za-z0-9_./-]+", body)
        endpoint = endpoint_match.group(0) if endpoint_match else None
        form_actions = extracted.get("form_actions") if isinstance(extracted.get("form_actions"), list) else []
        endpoint = endpoint or next((str(item) for item in form_actions if str(item).strip()), None)
        fields = [str(item) for item in (extracted.get("parameter_names") or []) if str(item)]
        if not fields:
            fields = ["asset_no", "department"] if "asset_no" in body and "department" in body else []
        control_values: dict[str, str] = {}
        control_match = re.search(r"(PC-[A-Za-z0-9_-]+)\s*/\s*([A-Z][A-Z0-9_-]+)", body)
        if control_match and len(fields) >= 2:
            control_values[fields[0]] = control_match.group(1)
            control_values[fields[1]] = control_match.group(2)
        if not endpoint and not fields:
            return {}
        return {
            "endpoint": endpoint,
            "method": "POST" if endpoint else "GET",
            "fields": fields,
            "control_values": control_values,
        }

    @staticmethod
    def _reduce_boolean(observation: SolverObservation) -> KnowledgeUpdate:
        true_value = _boolean_value(
            observation.raw_result,
            "true",
            "true_result",
            "true_signature",
            "stable_true",
        )
        false_value = _boolean_value(
            observation.raw_result,
            "false",
            "false_result",
            "false_signature",
            "stable_false",
        )
        if true_value is None:
            true_value = _stable_boolean_fallback(observation.raw_result, "stable_true")
        if false_value is None:
            false_value = _stable_boolean_fallback(observation.raw_result, "stable_false")
        oracle_fact = {
            "type": "BOOLEAN_ORACLE",
            "true": true_value,
            "false": false_value,
            "verified": False,
        }
        if observation.success and true_value is True and false_value is False:
            oracle_fact["verified"] = True
            tested_field = str(observation.raw_result.get("test_field") or "")
            tested_parameters = [
                str(item)
                for item in (observation.raw_result.get("tested_parameters") or [])
                if str(item)
            ]
            if tested_field and tested_field not in tested_parameters:
                tested_parameters.append(tested_field)
            return KnowledgeUpdate(
                verified_facts=[
                    oracle_fact,
                    {"type": "VALIDATION_SUCCESS", "verified": True},
                ],
                next_phase="EXPLOITATION",
                control_updates={
                    "validation_status": "VALIDATION_SUCCESS",
                    "strategy_needed": False,
                    "tested_parameter": tested_field or None,
                    "tested_parameters": tested_parameters,
                },
            )
        tested_field = observation.raw_result.get("test_field")
        tested_parameters = [
            str(item)
            for item in (observation.raw_result.get("tested_parameters") or [])
            if str(item)
        ]
        if tested_field and str(tested_field) not in tested_parameters:
            tested_parameters.append(str(tested_field))
        return KnowledgeUpdate(
            hypotheses=[
                {
                    "type": "VALIDATION_INCONCLUSIVE",
                    "true": true_value,
                    "false": false_value,
                    "strategy_needed": True,
                }
            ],
            next_phase="VALIDATION",
            control_updates={
                "validation_status": "VALIDATION_INCONCLUSIVE",
                "strategy_needed": True,
                "tested_parameter": tested_field,
                "tested_parameters": tested_parameters,
            },
        )

    @staticmethod
    def _reduce_metadata(observation: SolverObservation) -> KnowledgeUpdate:
        structured = observation.raw_result.get("structured_result")
        if not isinstance(structured, dict):
            structured = observation.raw_result
        extracted = structured.get("extracted_facts")
        if not isinstance(extracted, dict):
            extracted = structured.get("facts") if isinstance(structured.get("facts"), dict) else structured
        facts: list[dict[str, Any]] = []
        stage = str(structured.get("stage") or observation.raw_result.get("stage") or "metadata").lower()
        for key, fact_type in (
            ("database", "DATABASE_DISCOVERED"),
            ("version", "DB_VERSION_DISCOVERED"),
            ("version_comment", "DB_VERSION_COMMENT_DISCOVERED"),
            ("tables", "TABLES_DISCOVERED"),
            ("columns", "COLUMNS_DISCOVERED"),
        ):
            value = extracted.get(key)
            if value:
                facts.append({"type": fact_type, key: value, "verified": True})
        control_updates = {
            "metadata_stage": stage,
            "metadata_failure_increment": 0 if facts else 1,
            "metadata_last_status": "VERIFIED" if facts else "INCONCLUSIVE",
            "automation_terminal": False,
        }
        # These are monotonic recovery markers.  Do not write a false value
        # after a later tables/columns probe, otherwise the planner can loop
        # back to VERSION() indefinitely and never reach the fallback path.
        if stage == "version" or observation.raw_result.get("metadata_version_attempted") is True:
            control_updates["metadata_version_attempted"] = True
            control_updates["generic_fallback_pending"] = not bool(facts)
        return KnowledgeUpdate(
            verified_facts=facts,
            hypotheses=[] if facts else [{"type": "METADATA_INCONCLUSIVE", "stage": stage}],
            next_phase="EXPLOITATION" if observation.success and facts else "EXPLOITATION",
            control_updates=control_updates,
        )

    @staticmethod
    def _reduce_sqlite_metadata(observation: SolverObservation) -> KnowledgeUpdate:
        base = WebObservationReducer._reduce_metadata(observation)
        return KnowledgeUpdate(
            verified_facts=base.verified_facts,
            hypotheses=base.hypotheses,
            findings=base.findings,
            next_phase="EXPLOITATION",
            control_updates={
                **base.control_updates,
                "sqlite_attempted": True,
                "sqlite_stage": "VERIFIED" if base.verified_facts else "INCONCLUSIVE",
                "automation_terminal": False,
            },
        )

    @staticmethod
    def _reduce_calibration(observation: SolverObservation) -> KnowledgeUpdate:
        structured = observation.raw_result.get("structured_result")
        if not isinstance(structured, dict):
            structured = observation.raw_result
        profile = structured.get("adaptive_extraction_profile") or observation.raw_result.get("adaptive_extraction_profile")
        capabilities = structured.get("capabilities") if isinstance(structured.get("capabilities"), dict) else observation.raw_result.get("capabilities") if isinstance(observation.raw_result.get("capabilities"), dict) else {}
        if not observation.success or not isinstance(profile, dict) or not profile.get("extraction_strategy"):
            return KnowledgeUpdate(
                hypotheses=[{"type": "ORACLE_CALIBRATION_INCONCLUSIVE", "verified": False}],
                next_phase="EXPLOITATION",
                control_updates={"calibration_status": "INCONCLUSIVE"},
            )
        return KnowledgeUpdate(
            verified_facts=[
                {"type": "ORACLE_CALIBRATION_COMPLETED", "verified": True, "capabilities": capabilities},
                {"type": "ADAPTIVE_EXTRACTION_PROFILE", "verified": True, "profile": dict(profile)},
            ],
            next_phase="EXPLOITATION",
            control_updates={"calibration_status": "VERIFIED"},
        )

    @staticmethod
    def _reduce_extraction(observation: SolverObservation) -> KnowledgeUpdate:
        structured = observation.raw_result.get("structured_result")
        if not isinstance(structured, dict):
            structured = observation.raw_result
        value = structured.get("extracted_value")
        target_expression = str(structured.get("target_expression") or observation.raw_result.get("target_expression") or "")
        normalized_expression = target_expression.casefold()
        offset_match = re.search(r"\bOFFSET\s+(\d+)", target_expression, flags=re.IGNORECASE)
        expression_offset = int(offset_match.group(1)) if offset_match else 0
        if observation.success and value not in (None, "") and "information_schema.tables" in normalized_expression:
            names = [item.strip() for item in str(value).split(",") if item.strip()]
            if names:
                return KnowledgeUpdate(
                    verified_facts=[{"type": "TABLES_DISCOVERED", "tables": [{"name": item} for item in names], "verified": True}],
                    next_phase="EXPLOITATION",
                    control_updates={"generic_fallback_index": 1, "generic_fallback_source": "mysql"},
                )
        if observation.success and value not in (None, "") and "information_schema.columns" in normalized_expression:
            names = [item.strip() for item in str(value).split(",") if item.strip()]
            if names:
                return KnowledgeUpdate(
                    verified_facts=[{"type": "COLUMNS_DISCOVERED", "columns": [{"name": item} for item in names], "verified": True}],
                    next_phase="EXPLOITATION",
                    control_updates={"generic_fallback_done": True, "generic_fallback_pending": False},
                )
        if observation.success and value not in (None, "") and "select name from sqlite_master" in normalized_expression:
            names = [item.strip() for item in str(value).split(",") if item.strip()]
            if names:
                return KnowledgeUpdate(
                    verified_facts=[{"type": "TABLES_DISCOVERED", "tables": [{"name": item} for item in names], "verified": True}],
                    next_phase="EXPLOITATION",
                    control_updates={"generic_fallback_index": 2, "generic_fallback_source": "sqlite", "generic_table_offset": expression_offset + 1},
                )
        if observation.success and value not in (None, "") and "pragma_table_info" in normalized_expression:
            names = [item.strip() for item in str(value).split(",") if item.strip()]
            if names:
                return KnowledgeUpdate(
                    verified_facts=[{"type": "COLUMNS_DISCOVERED", "columns": [{"name": item} for item in names], "verified": True}],
                    next_phase="EXPLOITATION",
                    control_updates={"generic_fallback_index": 3, "generic_fallback_source": "sqlite", "generic_column_offset": expression_offset + 1, "generic_fallback_pending": True, "generic_fallback_done": False},
                )
        if "select name from sqlite_master" in normalized_expression and not value:
            return KnowledgeUpdate(
                hypotheses=[{"type": "SQLITE_TABLE_ENUMERATION_COMPLETE", "verified": True}],
                next_phase="EXPLOITATION",
                control_updates={"generic_fallback_index": 3, "generic_fallback_source": "sqlite", "generic_column_offset": 0, "generic_fallback_pending": True, "generic_fallback_done": False},
            )
        if "pragma_table_info" in normalized_expression and not value:
            return KnowledgeUpdate(
                hypotheses=[{"type": "SQLITE_COLUMN_ENUMERATION_COMPLETE", "verified": True}],
                next_phase="EXPLOITATION",
                control_updates={"generic_fallback_done": True, "generic_fallback_pending": False},
            )
        if "information_schema.tables" in normalized_expression and not value:
            return KnowledgeUpdate(
                hypotheses=[{"type": "MYSQL_SCHEMA_INCONCLUSIVE", "verified": False}],
                next_phase="EXPLOITATION",
                control_updates={
                    "generic_fallback_index": 1,
                    "generic_fallback_source": "sqlite",
                    "generic_fallback_pending": True,
                    "generic_fallback_done": False,
                },
            )
        # The generic SQLite aggregate is useful when supported, but some
        # oracle executors reject GROUP_CONCAT even though scalar SELECTs are
        # valid.  Preserve the recovery path and move to bounded scalar table
        # enumeration instead of treating the first executor rejection as a
        # terminal extraction result.
        if "sqlite_master" in normalized_expression and "group_concat" in normalized_expression:
            return KnowledgeUpdate(
                hypotheses=[{"type": "SQLITE_AGGREGATE_INCONCLUSIVE", "verified": False}],
                next_phase="EXPLOITATION",
                control_updates={
                    "generic_fallback_index": 2,
                    "generic_fallback_source": "sqlite",
                    "generic_table_offset": 0,
                    "generic_fallback_pending": True,
                    "generic_fallback_done": False,
                    "automation_terminal": False,
                },
            )
        if not observation.success or value in (None, "") or not observation.evidence_refs:
            return KnowledgeUpdate(
                hypotheses=[{"type": "EXTRACTION_INCONCLUSIVE", "verified": False}],
                next_phase="EXPLOITATION",
                control_updates={"extraction_status": "INCONCLUSIVE", "generic_fallback_done": True, "generic_fallback_pending": False, "automation_terminal": True},
            )
        return KnowledgeUpdate(
            findings=[
                {
                    "type": "VERIFIED_SQL_INJECTION_FINDING",
                    "title": "Boolean SQL injection produced a verified extracted value",
                    "verified": True,
                    "validation_status": "passed",
                    "evidence_refs": list(observation.evidence_refs),
                    "result": str(value),
                }
            ],
            verified_facts=[
                {
                    "type": "EXTRACTED_VALUE",
                    "verified": True,
                    "value": str(value),
                    "evidence_refs": list(observation.evidence_refs),
                }
            ],
            next_phase="REPORTING",
            control_updates={"extraction_status": "VERIFIED"},
        )

    @staticmethod
    def _reduce_request_capture(observation: SolverObservation) -> KnowledgeUpdate:
        structured = observation.raw_result.get("structured_result")
        if not isinstance(structured, dict):
            structured = observation.raw_result
        request_file = structured.get("request_file") or observation.raw_result.get("request_file")
        if not observation.success or not request_file:
            return KnowledgeUpdate(
                hypotheses=[{"type": "REQUEST_CAPTURE_INCONCLUSIVE", "verified": False}],
                next_phase="EXPLOITATION",
                control_updates={"request_captured": False},
            )
        return KnowledgeUpdate(
            verified_facts=[{"type": "REQUEST_CAPTURED", "request_file": str(request_file), "verified": True}],
            next_phase="EXPLOITATION",
            control_updates={"request_captured": True, "request_file": str(request_file)},
        )

    @staticmethod
    def _reduce_sqlmap_detect(observation: SolverObservation) -> KnowledgeUpdate:
        structured = observation.raw_result.get("structured_result")
        if not isinstance(structured, dict):
            structured = observation.raw_result
        injectable = structured.get("injectable")
        if not observation.success or injectable is False:
            return KnowledgeUpdate(
                hypotheses=[{"type": "SQLMAP_DETECTION_INCONCLUSIVE", "verified": False}],
                next_phase="EXPLOITATION",
                control_updates={"sqlmap_detected": False, "sqlmap_stage": "detect_failed", "automation_terminal": False},
            )
        return KnowledgeUpdate(
            verified_facts=[
                {
                    "type": "SQLMAP_DETECTION",
                    "injectable": injectable,
                    "parameter": structured.get("parameter"),
                    "dbms": structured.get("dbms"),
                    "verified": bool(injectable),
                }
            ],
            next_phase="EXPLOITATION",
            control_updates={"sqlmap_detected": True, "sqlmap_stage": "detected"},
        )

    @staticmethod
    def _reduce_sqlmap_run(observation: SolverObservation) -> KnowledgeUpdate:
        structured = observation.raw_result.get("structured_result")
        if not isinstance(structured, dict):
            structured = observation.raw_result
        action = str(structured.get("action") or observation.raw_result.get("action") or "dbs").lower()
        facts: list[dict[str, Any]] = []
        if structured.get("databases"):
            facts.append({"type": "SQLMAP_DATABASES", "databases": list(structured["databases"]), "verified": True})
        if structured.get("tables"):
            facts.append({"type": "SQLMAP_TABLES", "tables": list(structured["tables"]), "verified": True})
        if structured.get("columns"):
            facts.append({"type": "SQLMAP_COLUMNS", "columns": list(structured["columns"]), "verified": True})
        dumped_rows = structured.get("dumped_rows")
        extracted_value = structured.get("extracted_value")
        if observation.success and (dumped_rows or extracted_value not in (None, "")) and observation.evidence_refs:
            value = extracted_value if extracted_value not in (None, "") else dumped_rows
            return KnowledgeUpdate(
                verified_facts=[
                    *facts,
                    {"type": "SQLMAP_DUMP", "value": value, "verified": True, "evidence_refs": list(observation.evidence_refs)},
                ],
                findings=[{
                    "type": "VERIFIED_SQL_INJECTION_FINDING",
                    "title": "SQLMap produced an evidence-backed extracted value",
                    "verified": True,
                    "validation_status": "passed",
                    "evidence_refs": list(observation.evidence_refs),
                    "result": str(value),
                }],
                next_phase="REPORTING",
                control_updates={"sqlmap_stage": "completed", "extraction_status": "VERIFIED"},
            )
        if facts:
            next_stage = {"dbs": "databases", "tables": "tables", "columns": "columns"}.get(action, action)
            return KnowledgeUpdate(verified_facts=facts, next_phase="EXPLOITATION", control_updates={"sqlmap_stage": next_stage})
        return KnowledgeUpdate(
            hypotheses=[{"type": "SQLMAP_EXTRACTION_INCONCLUSIVE", "stage": action, "verified": False}],
            next_phase="EXPLOITATION",
            control_updates={"sqlmap_stage": f"{action}_inconclusive", "automation_terminal": True},
        )

    @staticmethod
    def _reduce_script_run(observation: SolverObservation) -> KnowledgeUpdate:
        structured = observation.raw_result.get("structured_result")
        if not isinstance(structured, dict):
            structured = observation.raw_result
        value = structured.get("extracted_value")
        tables = structured.get("tables") if isinstance(structured.get("tables"), list) else []
        columns = structured.get("columns") if isinstance(structured.get("columns"), list) else []
        facts: list[dict[str, Any]] = []
        if tables and observation.evidence_refs:
            facts.append({"type": "TABLES_DISCOVERED", "tables": [{"name": str(item)} for item in tables], "verified": True})
        if columns and observation.evidence_refs:
            facts.append({"type": "COLUMNS_DISCOVERED", "columns": [dict(item) if isinstance(item, dict) else {"name": str(item)} for item in columns], "verified": True})
        if observation.success and value not in (None, "") and observation.evidence_refs:
            return KnowledgeUpdate(
                verified_facts=[
                    *facts,
                    {
                        "type": "EXTRACTED_VALUE",
                        "value": str(value),
                        "candidate_table": structured.get("candidate_table"),
                        "candidate_column": structured.get("candidate_column"),
                        "verified": True,
                        "evidence_refs": list(observation.evidence_refs),
                    },
                ],
                findings=[
                    {
                        "type": "VERIFIED_SQL_INJECTION_FINDING",
                        "title": "Bounded Solver script produced an evidence-backed sensitive value",
                        "verified": True,
                        "validation_status": "passed",
                        "evidence_refs": list(observation.evidence_refs),
                        "result": str(value),
                        "table": structured.get("candidate_table"),
                        "column": structured.get("candidate_column"),
                    }
                ],
                next_phase="REPORTING",
                control_updates={
                    "script_attempted": True,
                    "extraction_status": "VERIFIED",
                    "automation_terminal": False,
                },
            )
        retryable = bool(structured.get("errors")) and int(structured.get("requests_sent") or 0) < 32
        return KnowledgeUpdate(
            verified_facts=facts,
            hypotheses=[{"type": "SCRIPT_EXTRACTION_INCONCLUSIVE", "verified": False}],
            next_phase="EXPLOITATION",
            control_updates={
                "script_attempted": True,
                "extraction_status": "INCONCLUSIVE",
                "automation_terminal": not retryable,
                "script_retry_pending": retryable,
                "script_retry_increment": 1 if retryable else 0,
            },
        )
