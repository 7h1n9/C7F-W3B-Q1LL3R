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
            true_signature = extracted.get("true_signature") or structured.get("true_signature") or ([true_rows[0].get("signature")] if true_rows and isinstance(true_rows[0], dict) else None)
            false_signature = extracted.get("false_signature") or structured.get("false_signature") or ([false_rows[0].get("signature")] if false_rows and isinstance(false_rows[0], dict) else None)
            differential = structured.get("true_false_differential", extracted.get("differential"))
            if structured.get("boolean_oracle_confirmed") or true_signature is not None or false_signature is not None or differential is not None:
                args = call.arguments_json or {}
                result.append({
                    "fact_key": f"asset_warranty.{args.get('test_field', 'field')}_boolean_oracle",
                    "fact_type": "BOOLEAN_ORACLE",
                    "value": {"true_signature": true_signature, "false_signature": false_signature, "repeat_stability": {"true": structured.get("stable_true"), "false": structured.get("stable_false")}, "response_differential": differential, "request_contract": args.get("request"), "test_field": args.get("test_field"), "baseline_value": args.get("baseline_value"), "control_fields": args.get("control_fields"), "oracle": args.get("oracle")},
                    "confidence": 95,
                })
        if call.tool_name == "sqlite_metadata_discovery":
            tables = extracted.get("tables") or structured.get("tables") or []
            columns = extracted.get("columns") or structured.get("columns") or []
            for table in tables if isinstance(tables, list) else []:
                name = str(table.get("name") if isinstance(table, dict) else table)
                if name:
                    result.append({"fact_key": f"sqlite.table.{name}", "fact_type": "SQL_TABLE", "value": {"table": name, "metadata": table}, "confidence": 90})
            for column in columns if isinstance(columns, list) else []:
                name = str(column.get("name") if isinstance(column, dict) else column)
                if name:
                    result.append({"fact_key": f"sqlite.column.{name}", "fact_type": "SQL_COLUMN", "value": {"column": name, "metadata": column}, "confidence": 90})
        return result


tool_result_fact_reducer = ToolResultFactReducer()
