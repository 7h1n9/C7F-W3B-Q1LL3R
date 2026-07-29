"""Compile semantic Analysis approvals into executable tool arguments.

Analysis is allowed to describe *what* should be tested.  This service is
the Controller boundary that decides the exact request shape, target URL,
provenance fields and tool-schema contract.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urljoin

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DomainError
from app.models.challenge import Challenge
from app.models.multi_agent import AnalysisReview, EvidenceLedger, PlannerProposal, VerifiedFact
from app.models.run import Hypothesis, SolveRun
from app.models.solver_state import SolverState
from app.schemas.multi_agent import CompiledApprovedAction
from app.tools.registry import ToolDefinition, load_tool_definitions

COMPILER_NAME = "approved_action_compiler"
COMPILER_VERSION = "v1"


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _json_payload(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return {}


class ApprovedActionCompiler:
    async def compile(
        self,
        session: AsyncSession,
        run: SolveRun,
        challenge: Challenge,
        proposal: PlannerProposal,
        review: AnalysisReview,
        tool_name: str,
    ) -> CompiledApprovedAction:
        definitions = load_tool_definitions()
        definition = definitions.get(tool_name)
        if definition is None or not definition.enabled:
            raise self._error(tool_name, "TOOL_NOT_AVAILABLE", {"tool": tool_name})
        try:
            arguments = await self._compile_arguments(session, run, challenge, proposal, review, tool_name, definition)
            validated = definition.validate_arguments(arguments)
        except DomainError as error:
            if error.code == "TOOL_INVALID_ARGUMENT":
                raise self._error(tool_name, "TOOL_SCHEMA_INVALID", error.details or {"message": error.message}) from error
            raise
        except Exception as error:
            raise self._error(tool_name, "COMPILER_EXCEPTION", {"message": str(error)[:1000]}) from error
        return CompiledApprovedAction(
            tool_name=tool_name,
            arguments=validated,
            arguments_digest=_digest(validated),
            tool_schema_hash=definition.schema_hash(),
            compiler_name=COMPILER_NAME,
            compiler_version=COMPILER_VERSION,
            source_review_id=review.id,
            source_proposal_id=proposal.id,
        )

    @staticmethod
    def _error(tool_name: str, reason: str, details: dict[str, Any]) -> DomainError:
        return DomainError(
            "APPROVED_ACTION_COMPILE_FAILED",
            "The Analysis approval could not be compiled into the current tool schema.",
            {"tool": tool_name, "reason": reason, **details},
            422,
        )

    @staticmethod
    def _asset_context(challenge: Challenge) -> dict[str, Any]:
        metadata = challenge.metadata_json or {}
        # Adapter selection is intentionally metadata-only.  Challenge names
        # are presentation data and must never select an execution adapter.
        adapter = str(metadata.get("adapter") or "")
        if adapter != "asset_warranty":
            return {}
        return {
            "adapter": adapter,
            "endpoint": str(metadata.get("endpoint") or ""),
            "method": str(metadata.get("method") or "POST").upper(),
            "content_type": str(metadata.get("content_type") or "application/json"),
            "fields": [str(item) for item in (metadata.get("fields") or [])],
            "control_values": _as_dict(metadata.get("control_values")),
        }

    @staticmethod
    def _target_url(challenge: Challenge, endpoint: str) -> str:
        if not challenge.target_url or not endpoint or not endpoint.startswith("/"):
            raise DomainError("APPROVED_ACTION_COMPILE_FAILED", "Challenge metadata does not contain an absolute target and endpoint.", {"target_url": challenge.target_url, "endpoint": endpoint})
        return urljoin(challenge.target_url.rstrip("/") + "/", endpoint.lstrip("/"))

    async def _compile_arguments(
        self,
        session: AsyncSession,
        run: SolveRun,
        challenge: Challenge,
        proposal: PlannerProposal,
        review: AnalysisReview,
        tool_name: str,
        definition: ToolDefinition,
    ) -> dict[str, Any]:
        semantic = _as_dict(review.approved_arguments_json)
        adapter = self._asset_context(challenge)
        if (challenge.metadata_json or {}).get("adapter") and not adapter:
            raise self._error(tool_name, "UNSUPPORTED_ADAPTER", {"adapter": (challenge.metadata_json or {}).get("adapter")})
        if tool_name == "http_request" and adapter:
            return self._asset_http_request(challenge, adapter, semantic, review)
        if tool_name == "http_request":
            request = _as_dict(semantic.get("request"))
            body = request.get("body", semantic.get("body"))
            json_body = request.get("json", semantic.get("json"))
            return {
                "method": str(request.get("method") or semantic.get("method") or "GET").upper(),
                "url": str(request.get("url") or semantic.get("url") or challenge.target_url or ""),
                **({"headers": _as_dict(request.get("headers") or semantic.get("headers"))} if request.get("headers") or semantic.get("headers") else {}),
                **({"body": body} if body is not None else {"json": json_body} if isinstance(json_body, dict) else {}),
            }
        if tool_name == "sql_boolean_compare" and adapter:
            return self._asset_boolean_compare(challenge, adapter, semantic, review)
        if tool_name == "sqlite_metadata_discovery":
            return await self._sqlite_metadata(session, run, proposal, review, semantic)
        if tool_name == "boolean_config_extract":
            return await self._boolean_extract(session, run, proposal, review, semantic)
        # Non-adapter tools still receive a strict, schema-validated copy of
        # the semantic argument object.  No budget or controller-only fields
        # are allowed to leak through this path.
        return {key: value for key, value in semantic.items() if key in definition.parameters}

    def _asset_http_request(self, challenge: Challenge, adapter: dict[str, Any], semantic: dict[str, Any], review: AnalysisReview) -> dict[str, Any]:
        request = _as_dict(semantic.get("request"))
        method = str(request.get("method") or semantic.get("method") or adapter["method"]).upper()
        endpoint = str(request.get("path") or semantic.get("path") or adapter["endpoint"])
        payload = _json_payload(
            request.get("json"),
            semantic.get("json"),
            request.get("body"),
            semantic.get("body"),
            adapter["control_values"],
        )
        if endpoint != adapter["endpoint"]:
            raise self._error("http_request", "ENDPOINT_NOT_FROM_CHALLENGE_METADATA", {"endpoint": endpoint, "expected": adapter["endpoint"]})
        if adapter["fields"] and set(payload) - set(adapter["fields"]):
            raise self._error("http_request", "UNKNOWN_METADATA_FIELD", {"unknown_fields": sorted(set(payload) - set(adapter["fields"]))})
        # A plan may explicitly select a control value, but cannot add fields
        # or silently change the target endpoint.
        headers = {"Content-Type": adapter["content_type"]}
        headers.update(_as_dict(request.get("headers") or semantic.get("headers")))
        return {"method": method, "url": self._target_url(challenge, endpoint), "headers": headers, "json": payload}

    def _asset_boolean_compare(self, challenge: Challenge, adapter: dict[str, Any], semantic: dict[str, Any], review: AnalysisReview) -> dict[str, Any]:
        oracle = _as_dict(semantic.get("oracle")) or {"json_field": "matched", "true_value": True, "false_value": False}
        field = str(semantic.get("test_field") or review.independent_variable or "")
        if not field:
            controls = _as_dict(review.required_controls_json)
            field = str(controls.get("test_field") or "")
        if not field or (adapter["fields"] and field not in adapter["fields"]):
            raise self._error("sql_boolean_compare", "TEST_FIELD_NOT_DECLARED", {"test_field": field, "fields": adapter["fields"]})
        baseline = semantic.get("baseline_value", adapter["control_values"].get(field))
        if baseline is None:
            raise self._error("sql_boolean_compare", "BASELINE_VALUE_MISSING", {"test_field": field})
        request_semantic = _as_dict(semantic.get("request"))
        payload = _json_payload(
            request_semantic.get("json"),
            semantic.get("json"),
            request_semantic.get("body"),
            semantic.get("body"),
            adapter["control_values"],
        )
        payload[field] = baseline
        control_fields = _as_dict(semantic.get("control_fields")) or {key: value for key, value in payload.items() if key != field}
        return {
            "request": {"method": adapter["method"], "url": self._target_url(challenge, adapter["endpoint"]), "headers": {"Content-Type": adapter["content_type"]}, "json": payload},
            "test_field": field,
            "baseline_value": str(baseline),
            "control_fields": control_fields,
            "true_condition": str(semantic.get("true_condition") or "' AND 1=1 -- "),
            "false_condition": str(semantic.get("false_condition") or "' AND 1=2 -- "),
            "oracle": {"json_field": str(oracle.get("json_field") or "matched"), "true_value": oracle.get("true_value", True), "false_value": oracle.get("false_value", False)},
            "max_requests": max(5, int(semantic.get("max_requests") or 5)),
        }

    async def _oracle_sources(self, session: AsyncSession, run: SolveRun) -> tuple[dict[str, Any], list[str], list[str]]:
        state = await session.scalar(select(SolverState).where(SolverState.run_id == run.id))
        ledger = state.capability_ledger_json if state else {}
        payload = _as_dict(ledger.get("boolean_oracle_confirmed"))
        request = _as_dict(payload.get("request_spec"))
        evidence_ids = list(payload.get("evidence_ids") or [])
        fact_ids = list(payload.get("fact_ids") or [])
        if not request or not payload.get("test_field") or not payload.get("oracle"):
            raise self._error("sqlite_metadata_discovery", "BOOLEAN_ORACLE_REQUIRED", {"capability": "boolean_oracle_confirmed"})
        return {"request": request, "test_field": payload["test_field"], "baseline_value": payload.get("baseline_value"), "control_fields": payload.get("control_fields") or {}, "oracle": payload["oracle"]}, evidence_ids, fact_ids

    async def _sqlite_metadata(self, session: AsyncSession, run: SolveRun, proposal: PlannerProposal, review: AnalysisReview, semantic: dict[str, Any]) -> dict[str, Any]:
        oracle, evidence_ids, fact_ids = await self._oracle_sources(session, run)
        if not evidence_ids or not fact_ids:
            raise self._error("sqlite_metadata_discovery", "BOOLEAN_ORACLE_PROVENANCE_INCOMPLETE", {"evidence_ids": evidence_ids, "fact_ids": fact_ids})
        target = str(semantic.get("target_expression") or "").strip()
        if not target:
            raise self._error("sqlite_metadata_discovery", "SQL_EXPRESSION_PROVENANCE_REQUIRED", {"target_expression": "required"})
        source_hypothesis_id = str(semantic.get("source_hypothesis_id") or "")
        await self._validate_sql_sources(session, run, evidence_ids, fact_ids, source_hypothesis_id)
        return {
            "request": oracle["request"], "test_field": oracle["test_field"], "baseline_value": str(oracle.get("baseline_value") or ""),
            "target_expression": target, "expression_type": str(semantic.get("expression_type") or "METADATA_DISCOVERY"),
            "supporting_evidence_ids": list(semantic.get("supporting_evidence_ids") or evidence_ids),
            "supporting_fact_ids": list(semantic.get("supporting_fact_ids") or fact_ids),
            "source_hypothesis_id": source_hypothesis_id,
            "approved_analysis_review_id": review.id,
            "assumption_status": str(semantic.get("assumption_status") or "VERIFIED"),
            "max_requests": int(semantic.get("max_requests") or 5), "resume": bool(semantic.get("resume", False)),
        }

    async def _boolean_extract(self, session: AsyncSession, run: SolveRun, proposal: PlannerProposal, review: AnalysisReview, semantic: dict[str, Any]) -> dict[str, Any]:
        oracle, evidence_ids, fact_ids = await self._oracle_sources(session, run)
        if not evidence_ids or not fact_ids:
            raise self._error("boolean_config_extract", "BOOLEAN_ORACLE_PROVENANCE_INCOMPLETE", {"evidence_ids": evidence_ids, "fact_ids": fact_ids})
        target = str(semantic.get("target_expression") or "").strip()
        if not target:
            fact = await session.scalar(select(VerifiedFact).where(VerifiedFact.run_id == run.id, VerifiedFact.fact_type.in_(["SQL_EXPRESSION", "SQL_COLUMN", "SQL_SCHEMA"]), VerifiedFact.promotion_status == "VERIFIED").order_by(VerifiedFact.updated_at.desc()))
            if fact and isinstance(fact.value_json, dict):
                target = str(fact.value_json.get("target_expression") or "").strip()
        if not target:
            raise self._error("boolean_config_extract", "SQL_EXPRESSION_PROVENANCE_REQUIRED", {"target_expression": "must come from a verified fact or review"})
        source_hypothesis_id = str(semantic.get("source_hypothesis_id") or "")
        await self._validate_sql_sources(session, run, evidence_ids, fact_ids, source_hypothesis_id)
        return {
            "request": oracle["request"], "test_field": oracle["test_field"], "baseline_value": str(oracle.get("baseline_value") or ""),
            "target_expression": target, "oracle": oracle["oracle"], "control_fields": oracle["control_fields"],
            "max_requests": int(semantic.get("max_requests") or 1024), "max_length": int(semantic.get("max_length") or 128),
            "expression_type": str(semantic.get("expression_type") or "VALUE_EXTRACTION"),
            "supporting_evidence_ids": list(semantic.get("supporting_evidence_ids") or evidence_ids),
            "supporting_fact_ids": list(semantic.get("supporting_fact_ids") or fact_ids),
            "source_hypothesis_id": source_hypothesis_id,
            "approved_analysis_review_id": review.id, "assumption_status": str(semantic.get("assumption_status") or "VERIFIED"),
        }

    @staticmethod
    async def _validate_sql_sources(session: AsyncSession, run: SolveRun, evidence_ids: list[str], fact_ids: list[str], hypothesis_id: str) -> None:
        evidence = list((await session.scalars(select(EvidenceLedger).where(EvidenceLedger.run_id == run.id, EvidenceLedger.id.in_(evidence_ids)))).all())
        facts = list((await session.scalars(select(VerifiedFact).where(VerifiedFact.run_id == run.id, VerifiedFact.id.in_(fact_ids)))).all())
        hypothesis = await session.scalar(select(Hypothesis).where(Hypothesis.run_id == run.id, Hypothesis.id == hypothesis_id))
        if len(evidence) != len(set(evidence_ids)) or len(facts) != len(set(fact_ids)) or hypothesis is None:
            raise DomainError("APPROVED_ACTION_COMPILE_FAILED", "SQL expression provenance does not resolve to this Run's durable records.", {"evidence_ids": evidence_ids, "fact_ids": fact_ids, "source_hypothesis_id": hypothesis_id})


approved_action_compiler = ApprovedActionCompiler()
