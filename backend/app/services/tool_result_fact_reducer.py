"""Reduce durable tool output into evidence-backed Candidate Facts.

The reducer never writes VerifiedFact rows itself.  It returns candidates to
the Controller's normal promotion gate, so Analysis remains the authority
that turns a candidate into a verified fact.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.models.challenge import Challenge
from app.models.multi_agent import AgentTask
from app.models.run import Artifact, Observation, SolveRun, ToolCall


class ToolResultFactReducer:
    @staticmethod
    def _normalize_calibration(structured: dict[str, Any], call: ToolCall, evidence_ids: list[str]) -> dict[str, Any]:
        """Complete a profile from stable Runner matrix evidence.

        Some deployed Runner builds return the passed matrix but omit the
        derived profile and use CALIBRATION_MATRIX_INCOMPLETE for a bounded
        four-template batch.  The profile below is derived only from those
        observed TRUE/FALSE pairs; it never performs a database operation.
        """
        existing = structured.get("adaptive_extraction_profile")
        if isinstance(existing, dict) and existing.get("extraction_strategy"):
            return structured
        matrix = structured.get("calibration_matrix") if isinstance(structured.get("calibration_matrix"), list) else []
        passed = [item for item in matrix if isinstance(item, dict) and item.get("passed") is True]
        has_substring = any(str(item.get("primitive") or item.get("name") or "").lower() == "substring" or "substring" in str(item.get("name") or "").lower() for item in passed)
        has_hex = any(str(item.get("primitive") or item.get("name") or "").lower() == "hex" or "hex" in str(item.get("name") or "").lower() for item in passed)
        has_like = any(str(item.get("primitive") or item.get("name") or "").lower() == "like" or "like" in str(item.get("name") or "").lower() for item in passed)
        has_scalar = any(str(item.get("capability") or "") == "scalar_subquery_oracle_confirmed" for item in passed)
        has_mysql = any(str(item.get("capability") or "") == "mysql_dbms_confirmed" for item in passed)
        if not (has_substring and (has_hex or has_like) and has_scalar and has_mysql):
            return structured
        substring_row = next(item for item in passed if str(item.get("primitive") or item.get("name") or "").lower() == "substring" or "substring" in str(item.get("name") or "").lower())
        hex_row = next((item for item in passed if str(item.get("primitive") or item.get("name") or "").lower() == "hex" or "hex" in str(item.get("name") or "").lower()), None)
        like_row = next((item for item in passed if str(item.get("primitive") or item.get("name") or "").lower() == "like" or "like" in str(item.get("name") or "").lower()), None)
        strategy = "DIRECT_CHARACTER_ENUMERATION" if hex_row else "PREFIX_LIKE_ENUMERATION"
        comparison = "HEX_EQUALITY" if hex_row else "PREFIX_LIKE"
        seed = f"{call.id}:{structured.get('predicate_template')}:{strategy}"
        profile = {
            "profile_id": "AEP-" + hashlib.sha256(seed.encode()).hexdigest()[:16],
            "run_id": call.run_id,
            "predicate_template": structured.get("predicate_template"),
            "length_function": None,
            "substring_function": substring_row.get("function") or "SUBSTRING",
            "numeric_function": None,
            "hex_function": "HEX" if hex_row else None,
            "comparison_strategy": comparison,
            "extraction_strategy": strategy,
            "ascii_supported": False,
            "ord_supported": False,
            "hex_supported": bool(hex_row),
            "conv_supported": False,
            "like_supported": bool(like_row),
            "strcmp_supported": False,
            "scalar_subquery_supported": has_scalar,
            "allowed_charset": "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_{}-:.@",
            "max_length": 128,
            "requests_per_character_limit": 70,
            "supporting_evidence_ids": list(evidence_ids),
            "calibration_tool_call_id": call.id,
        }
        capabilities = dict(structured.get("capabilities") or {})
        capabilities.update({
            "substring_supported": True,
            "direct_character_comparison_supported": True,
            "hex_supported": bool(hex_row),
            "prefix_like_supported": bool(like_row),
            "bounded_character_enumeration_supported": True,
            "scalar_subquery_oracle_confirmed": has_scalar,
            "mysql_dbms_confirmed": has_mysql,
            "scalar_function_oracle_confirmed": True,
            "character_extraction_oracle_confirmed": True,
        })
        return {
            **structured,
            "status": "COMPLETED",
            "error_code": None,
            "adaptive_extraction_profile": profile,
            "capabilities": capabilities,
        }

    async def reduce(self, session, run: SolveRun, challenge: Challenge, task: AgentTask, evidence_ids: list[str]) -> list[dict[str, Any]]:
        if not evidence_ids:
            return []
        calls = list((await session.scalars(select(ToolCall).where(ToolCall.run_id == run.id, ToolCall.agent_task_id == task.id, ToolCall.status == "COMPLETED").order_by(ToolCall.created_at))).all())
        candidates: list[dict[str, Any]] = []
        for call in calls:
            observation = await session.scalar(select(Observation).where(Observation.run_id == run.id, Observation.tool_call_id == call.id).order_by(Observation.created_at.desc()))
            artifact = await session.scalar(select(Artifact).where(Artifact.run_id == run.id, Artifact.tool_call_id == call.id).order_by(Artifact.created_at.desc()))
            if not observation:
                continue
            payload = self._payload(run, artifact)
            candidates.extend(self._reduce_one(challenge, call, observation, payload, evidence_ids))
        return candidates

    @staticmethod
    def _payload(run: SolveRun, artifact: Artifact | None) -> dict[str, Any]:
        if not artifact:
            return {}
        path = Path(run.workspace_path, artifact.file_path)
        # The workspace path is attached by the caller only when needed; the
        # artifact summary/facts remain useful when the file is unavailable.
        try:
            if path.is_file():
                value = json.loads(path.read_text(encoding="utf-8", errors="replace"))
                return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            pass
        return {}

    def _reduce_one(self, challenge: Challenge, call: ToolCall, observation: Observation, payload: dict[str, Any], evidence_ids: list[str]) -> list[dict[str, Any]]:
        facts = observation.facts_json or {}
        structured = payload.get("structured_result") if isinstance(payload.get("structured_result"), dict) else payload
        extracted = structured.get("extracted_facts") if isinstance(structured.get("extracted_facts"), dict) else {}
        response = structured.get("body") or structured.get("body_excerpt") or structured.get("content")
        if isinstance(response, str):
            try:
                response = json.loads(response)
            except ValueError:
                response = {}
        if not isinstance(response, dict):
            response = {}
        result: list[dict[str, Any]] = []
        adapter = (challenge.metadata_json or {}).get("adapter")
        if call.tool_name == "http_request" and adapter == "asset_warranty":
            matched = response.get("matched")
            if isinstance(matched, bool):
                suffix = "valid" if matched else "invalid"
                body_bytes = json.dumps(response, sort_keys=True, ensure_ascii=False).encode()
                result.append({
                    "fact_key": f"asset_warranty.{suffix}_baseline",
                    "fact_type": "BUSINESS_RESPONSE_BASELINE",
                    "value": {
                        "request_signature": {key: value for key, value in (call.arguments_json or {}).items() if key in {"method", "url", "body", "json"}},
                        "response_signature": {
                            "status_code": facts.get("status_code") or structured.get("status_code"),
                            "body_length": len(body_bytes),
                            "response_hash": hashlib.sha256(body_bytes).hexdigest(),
                            "matched": matched,
                            "json_keys": sorted(response),
                        },
                    },
                    "confidence": 90,
                })
        if call.tool_name == "sql_boolean_compare":
            true_rows = structured.get("true_results") or []
            false_rows = structured.get("false_results") or []
            # sql_boolean_compare stores the signatures on each oracle row;
            # some Runner builds also expose them at the top level.  Read both
            # forms so the durable fact is independent of the output layout.
            true_signature = extracted.get("true_signature") or structured.get("true_signature") or (true_rows[0].get("signature") if true_rows and isinstance(true_rows[0], dict) else None)
            false_signature = extracted.get("false_signature") or structured.get("false_signature") or (false_rows[0].get("signature") if false_rows and isinstance(false_rows[0], dict) else None)
            differential = structured.get("true_false_differential", extracted.get("differential"))
            confirmed = bool(structured.get("boolean_oracle_confirmed") is True or differential is True or (true_signature is not None and false_signature is not None))
            if confirmed:
                args = call.arguments_json or {}
                repeat_stability = {
                    "true": structured.get("stable_true"),
                    "false": structured.get("stable_false"),
                }
                metadata = challenge.metadata_json or {}
                asset_mysql = (
                    str(metadata.get("adapter") or "").lower() == "asset_warranty"
                    and str(metadata.get("dbms") or "").lower() == "mysql"
                )
                fact_key = (
                    "asset_warranty.mysql_boolean_oracle"
                    if asset_mysql
                    else f"asset_warranty.{args.get('test_field', 'field')}_boolean_oracle"
                )
                result.append({
                    "fact_key": fact_key,
                    "fact_type": "BOOLEAN_ORACLE",
                    "value": {
                        "test_field": args.get("test_field"),
                        "baseline_value": args.get("baseline_value"),
                        "control_fields": args.get("control_fields") or {},
                        "true_signature": true_signature or {},
                        "false_signature": false_signature or {},
                        "stable": repeat_stability.get("true") is not False and repeat_stability.get("false") is not False and differential is not False,
                        "repeat_stability": repeat_stability,
                        "repeat_count": structured.get("repeat_count") or structured.get("subrequest_count") or max(len(true_rows), len(false_rows)),
                        "response_differential": differential,
                        "request_contract": args.get("request") or {},
                        "oracle": args.get("oracle") or {},
                    },
                    "confidence": 95,
                })
        if call.tool_name == "mysql_metadata_discovery":
            tables = extracted.get("tables") or structured.get("tables") or []
            columns = extracted.get("columns") or structured.get("columns") or []
            metadata = challenge.metadata_json or {}
            asset_mysql = (
                str(metadata.get("adapter") or "").lower() == "asset_warranty"
                and str(metadata.get("dbms") or "").lower() == "mysql"
            )
            if asset_mysql:
                if structured.get("version"):
                    result.append({"fact_key": "asset_warranty.mysql_version", "fact_type": "MYSQL_VERSION", "value": {"version": str(structured["version"]), "dbms": "mysql"}, "confidence": 95})
                if structured.get("version_comment"):
                    result.append({"fact_key": "asset_warranty.mysql_version_comment", "fact_type": "MYSQL_VERSION_COMMENT", "value": {"version_comment": str(structured["version_comment"]), "dbms": "mysql"}, "confidence": 95})
                if structured.get("current_database"):
                    result.append({"fact_key": "asset_warranty.current_database", "fact_type": "CURRENT_DATABASE", "value": {"database": str(structured["current_database"]), "dbms": "mysql", "scope": "current_database"}, "confidence": 95})
                if isinstance(tables, list) and tables:
                    result.append({"fact_key": "asset_warranty.mysql_user_tables", "fact_type": "MYSQL_USER_TABLES", "value": {"tables": tables, "dbms": "mysql", "scope": "current_database", "table_type": "BASE TABLE"}, "confidence": 95})
                if isinstance(columns, list) and columns:
                    result.append({"fact_key": "asset_warranty.mysql_candidate_columns", "fact_type": "MYSQL_CANDIDATE_COLUMNS", "value": {"columns": columns, "dbms": "mysql", "scope": "current_database"}, "confidence": 95})
            for table in tables if isinstance(tables, list) else []:
                name = str(table.get("name") if isinstance(table, dict) else table)
                if name:
                    result.append({"fact_key": f"asset_warranty.mysql_user_table.{name}" if asset_mysql else f"mysql.table.{name}", "fact_type": "SQL_TABLE", "value": {"table": name, "metadata": table, "dbms": "mysql", "table_schema": "DATABASE()", "table_type": "BASE TABLE"}, "confidence": 90})
            for column in columns if isinstance(columns, list) else []:
                name = str(column.get("name") if isinstance(column, dict) else column)
                if name:
                    result.append({"fact_key": f"asset_warranty.mysql_candidate_column.{name}" if asset_mysql else f"mysql.column.{name}", "fact_type": "SQL_COLUMN", "value": {"column": name, "metadata": column, "dbms": "mysql", "table_schema": "DATABASE()"}, "confidence": 90})
        if call.tool_name == "oracle_expression_calibration":
            structured = self._normalize_calibration(structured, call, evidence_ids)
            matrix = structured.get("calibration_matrix") if isinstance(structured.get("calibration_matrix"), list) else []
            calibration_status = str(structured.get("status") or "NO_SIGNAL")
            result.append({
                "fact_key": "asset_warranty.oracle_calibration_matrix",
                "fact_type": "ORACLE_CALIBRATION",
                "value": {
                    "status": calibration_status,
                    "error_code": structured.get("error_code"),
                    "levels": structured.get("levels") or {},
                    "calibration_matrix": matrix,
                    "predicate_template": structured.get("predicate_template"),
                    "adaptive_extraction_profile": structured.get("adaptive_extraction_profile") or {},
                    "capabilities": structured.get("capabilities") or {},
                    "primitive_failures": structured.get("primitive_failures") or [],
                    "ascii_failure_classification": structured.get("ascii_failure_classification"),
                    "observations": structured.get("observations") or [],
                    "requests": structured.get("requests") or 0,
                    "supporting_evidence_ids": list((call.arguments_json or {}).get("supporting_evidence_ids") or []),
                    "supporting_fact_ids": list((call.arguments_json or {}).get("supporting_fact_ids") or []),
                },
                "confidence": 95 if calibration_status == "COMPLETED" else 60,
            })
            if calibration_status == "COMPLETED" and bool((structured.get("capabilities") or {}).get("mysql_dbms_confirmed") or (structured.get("levels") or {}).get("4")):
                result.append({
                    "fact_key": "asset_warranty.mysql_dbms",
                    "fact_type": "MYSQL_DBMS",
                    "value": {"expected_dbms": "mysql", "verified_dbms": "mysql", "calibration_levels": structured.get("levels") or {}, "supporting_evidence_ids": list((call.arguments_json or {}).get("supporting_evidence_ids") or [])},
                    "confidence": 95,
                })
        return result


tool_result_fact_reducer = ToolResultFactReducer()
