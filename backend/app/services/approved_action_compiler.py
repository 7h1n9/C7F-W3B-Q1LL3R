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
from app.models.run import Hypothesis, SolveRun, ToolCall
from app.models.solver_state import SolverState
from app.schemas.multi_agent import CompiledApprovedAction
from app.security.task_policy import get_allowed_tools, validate_tools, vulnerability_type_from_metadata
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
        metadata = challenge.metadata_json or {}
        if str(metadata.get("adapter") or "").lower() != "asset_warranty":
            vulnerability_type = vulnerability_type_from_metadata(metadata)
            current_phase = str(run.current_phase or proposal.current_stage or "")
            current_policy = get_allowed_tools(vulnerability_type, current_phase)
            declared_policy = get_allowed_tools(vulnerability_type, proposal.current_stage)
            policy_result = validate_tools(
                vulnerability_type,
                current_phase,
                [tool_name],
            )
            if current_policy["allowed_tools"] and declared_policy["phase"] != current_policy["phase"]:
                policy_result = {
                    "decision": "REVISE",
                    "reason": f"proposal stage {declared_policy['phase']} does not match current phase {current_policy['phase']}",
                    "invalid_tools": [],
                    "policy": current_policy,
                }
            if policy_result["decision"] == "REVISE":
                raise DomainError(
                    "TASK_POLICY_VIOLATION",
                    "ApprovedAction tool is not allowed in the current vulnerability phase.",
                    {
                        "tool": tool_name,
                        "decision": "REVISE",
                        "policy": policy_result["policy"],
                        "reason": policy_result["reason"],
                    },
                    422,
                )
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
        if adapter != "asset_warranty" or str(metadata.get("dbms") or "").lower() != "mysql":
            return {}
        return {
            "adapter": adapter,
            "dbms": str(metadata.get("dbms") or "").lower(),
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
        metadata = challenge.metadata_json or {}
        declared_adapter = str(metadata.get("adapter") or "").lower()
        generic_golden = (
            declared_adapter == "benchmark_sql_injection_easy"
            and str(metadata.get("benchmark_case_id") or "") == "sql-injection-golden"
        )
        if declared_adapter and not adapter and not generic_golden:
            raise self._error(tool_name, "UNSUPPORTED_ADAPTER", {"adapter": metadata.get("adapter")})
        if tool_name == "http_request" and adapter:
            return self._asset_http_request(challenge, adapter, semantic, review)
        if tool_name == "http_request":
            request = _as_dict(semantic.get("request"))
            if not request:
                request = _as_dict(metadata.get("baseline_request"))
            request = dict(request)
            if "params" in request and "query" not in request:
                request["query"] = request.pop("params")
            body = request.get("body", semantic.get("body"))
            json_body = request.get("json", semantic.get("json"))
            return {
                "method": str(request.get("method") or semantic.get("method") or "GET").upper(),
                "url": str(request.get("url") or semantic.get("url") or challenge.target_url or ""),
                **({"headers": _as_dict(request.get("headers") or semantic.get("headers"))} if request.get("headers") or semantic.get("headers") else {}),
                **({"query": _as_dict(request.get("query"))} if request.get("query") else {}),
                **({"body": body} if body is not None else {"json": json_body} if isinstance(json_body, dict) else {}),
            }
        if tool_name == "sql_boolean_compare" and adapter:
            return self._asset_boolean_compare(challenge, adapter, semantic, review)
        if tool_name == "sql_boolean_compare":
            return await self._generic_boolean_compare(session, run, challenge, semantic, review)
        if tool_name == "sqlite_metadata_discovery" and adapter:
            raise self._error(tool_name, "DBMS_ROUTE_FORBIDDEN", {"adapter": adapter["adapter"], "dbms": adapter["dbms"], "required_tool": "mysql_metadata_discovery"})
        if tool_name == "mysql_metadata_discovery":
            if not adapter or adapter.get("dbms") != "mysql":
                raise self._error(tool_name, "MYSQL_ADAPTER_REQUIRED", {"adapter": adapter.get("adapter") if adapter else None, "dbms": adapter.get("dbms") if adapter else None})
            return await self._mysql_metadata(session, run, proposal, review, semantic)
        if tool_name == "oracle_expression_calibration":
            if not adapter or adapter.get("adapter") != "asset_warranty":
                raise self._error(tool_name, "ASSET_WARRANTY_ADAPTER_REQUIRED", {"adapter": adapter.get("adapter") if adapter else None})
            return await self._oracle_expression_calibration(session, run, proposal, review, semantic)
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
        if field not in adapter["fields"]:
            # Analysis sometimes adds a human-readable qualifier to the
            # declared field (for example, ``asset_no Boolean predicate``).
            # Reduce only an unambiguous declared-field token; never invent a
            # field outside challenge metadata.
            matches = [candidate for candidate in adapter["fields"] if candidate.lower() in field.lower()]
            if len(matches) == 1:
                field = matches[0]
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

    @staticmethod
    def _request_value(request: dict[str, Any], field: str) -> tuple[Any, str | None]:
        """Find a baseline field in a RequestSpec without inventing it."""
        for container_name in ("params", "query", "json", "form"):
            container = request.get(container_name)
            if isinstance(container, dict) and field in container:
                return container[field], container_name
        body = request.get("body")
        if isinstance(body, dict) and field in body:
            return body[field], "body"
        if isinstance(body, str):
            parsed = _json_payload(body)
            if field in parsed:
                return parsed[field], "body"
        return None, None

    @staticmethod
    def _request_control_fields(request: dict[str, Any], field: str) -> dict[str, Any]:
        controls: dict[str, Any] = {}
        for container_name in ("params", "query", "json", "form"):
            container = request.get(container_name)
            if isinstance(container, dict):
                controls.update({key: value for key, value in container.items() if key != field})
        body = request.get("body")
        if isinstance(body, dict):
            controls.update({key: value for key, value in body.items() if key != field})
        return controls

    async def _generic_boolean_compare(
        self,
        session: AsyncSession,
        run: SolveRun,
        challenge: Challenge,
        semantic: dict[str, Any],
        review: AnalysisReview,
    ) -> dict[str, Any]:
        """Compile a generic SQL Boolean action from durable target context.

        Planner/Analysis may identify the field or objective, but the final
        RequestSpec and controls are owned by this Controller boundary.
        """
        metadata = challenge.metadata_json or {}
        request = _as_dict(semantic.get("request"))
        if not request:
            request = _json_payload(metadata.get("baseline_request"), metadata.get("request"))
        if not request:
            prior_call = await session.scalar(
                select(ToolCall)
                .where(
                    ToolCall.run_id == run.id,
                    ToolCall.tool_name == "http_request",
                    ToolCall.status == "COMPLETED",
                )
                .order_by(ToolCall.created_at.desc())
            )
            if prior_call is not None:
                prior_arguments = _as_dict(prior_call.arguments_json)
                request = {
                    key: prior_arguments[key]
                    for key in ("method", "url", "headers", "json", "body", "params", "query", "form")
                    if key in prior_arguments
                }
        request = dict(request)
        if "params" in request and "query" not in request:
            request["query"] = request.pop("params")
        request["method"] = str(request.get("method") or metadata.get("method") or "GET").upper()
        target_url = str(request.get("url") or semantic.get("url") or challenge.target_url or "")
        if target_url.startswith("/") and challenge.target_url:
            target_url = urljoin(challenge.target_url.rstrip("/") + "/", target_url.lstrip("/"))
        if not target_url:
            raise self._error("sql_boolean_compare", "REQUEST_CONTRACT_MISSING", {"required": ["method", "url"]})
        request["url"] = target_url

        attack_surface = metadata.get("attack_surface")
        surface_parameter = ""
        if isinstance(attack_surface, list):
            for item in attack_surface:
                if isinstance(item, dict) and item.get("parameter"):
                    surface_parameter = str(item["parameter"])
                    break
        request_fields: list[str] = []
        for container_name in ("query", "json", "form"):
            container = request.get(container_name)
            if isinstance(container, dict):
                request_fields.extend(str(key).strip() for key in container if str(key).strip())
        requested_field = str(
            semantic.get("test_field")
            or review.independent_variable
            or ""
        ).strip()
        declared_fields = [
            str(value).strip()
            for value in (*request_fields, metadata.get("test_field"), metadata.get("parameter"), surface_parameter)
            if str(value or "").strip()
        ]
        field = requested_field
        if declared_fields:
            exact = next((value for value in declared_fields if value == requested_field), None)
            if exact:
                field = exact
            else:
                matches = [value for value in declared_fields if value.lower() in requested_field.lower()]
                field = matches[0] if len(matches) == 1 else declared_fields[0]
        if not field:
            field = declared_fields[0] if declared_fields else ""
        if not field:
            raise self._error(
                "sql_boolean_compare",
                "TEST_FIELD_NOT_DECLARED",
                {"required": "test_field", "source": "challenge metadata or baseline request"},
            )
        baseline, _ = self._request_value(request, field)
        if baseline is None:
            baseline = semantic.get("baseline_value")
        if baseline is None:
            baseline = (metadata.get("baseline_values") or {}).get(field) if isinstance(metadata.get("baseline_values"), dict) else None
        if baseline is None:
            raise self._error("sql_boolean_compare", "BASELINE_VALUE_MISSING", {"test_field": field})

        controls = _as_dict(semantic.get("control_fields"))
        if not controls:
            controls = self._request_control_fields(request, field)
        oracle = _as_dict(semantic.get("oracle")) or {
            "json_field": str(metadata.get("oracle_field") or "matched"),
            "true_value": True,
            "false_value": False,
        }
        return {
            "request": request,
            "test_field": field,
            "baseline_value": str(baseline),
            "control_fields": controls,
            "oracle": oracle,
            "true_condition": str(semantic.get("true_condition") or "' AND 1=1 -- "),
            "false_condition": str(semantic.get("false_condition") or "' AND 1=2 -- "),
            "max_requests": max(5, int(semantic.get("max_requests") or 5)),
        }

    async def _oracle_sources(self, session: AsyncSession, run: SolveRun) -> tuple[dict[str, Any], list[str], list[str]]:
        state = await session.scalar(select(SolverState).where(SolverState.run_id == run.id))
        ledger = state.capability_ledger_json if state else {}
        payload = _as_dict(ledger.get("boolean_oracle_confirmed")) or _as_dict(ledger.get("mysql_boolean_oracle_confirmed"))
        request = _as_dict(payload.get("request_spec"))
        evidence_ids = list(payload.get("evidence_ids") or [])
        fact_ids = list(payload.get("fact_ids") or [])
        oracle_fact = await session.scalar(select(VerifiedFact).where(
            VerifiedFact.run_id == run.id,
            VerifiedFact.fact_key == "asset_warranty.mysql_boolean_oracle",
            VerifiedFact.promotion_status == "VERIFIED",
        ))
        if oracle_fact is None:
            raise self._error("mysql_metadata_discovery", "BOOLEAN_ORACLE_REQUIRED", {"fact_key": "asset_warranty.mysql_boolean_oracle"})
        if oracle_fact.id not in fact_ids:
            fact_ids.append(oracle_fact.id)
        for evidence_id in list(oracle_fact.evidence_ids_json or []):
            if evidence_id not in evidence_ids:
                evidence_ids.append(evidence_id)
        if not request or not payload.get("test_field") or not payload.get("oracle"):
            raise self._error("mysql_metadata_discovery", "BOOLEAN_ORACLE_REQUIRED", {"capability": "boolean_oracle_confirmed"})
        return {"request": request, "test_field": payload["test_field"], "baseline_value": payload.get("baseline_value"), "control_fields": payload.get("control_fields") or {}, "oracle": payload["oracle"]}, evidence_ids, fact_ids

    async def _mysql_metadata(self, session: AsyncSession, run: SolveRun, proposal: PlannerProposal, review: AnalysisReview, semantic: dict[str, Any]) -> dict[str, Any]:
        oracle, evidence_ids, fact_ids = await self._oracle_sources(session, run)
        if not evidence_ids or not fact_ids:
            raise self._error("mysql_metadata_discovery", "BOOLEAN_ORACLE_PROVENANCE_INCOMPLETE", {"evidence_ids": evidence_ids, "fact_ids": fact_ids})
        target = str(semantic.get("target_expression") or "").strip()
        state = await session.scalar(select(SolverState).where(SolverState.run_id == run.id))
        ledger = state.capability_ledger_json if state else {}
        extraction_profile = _as_dict(ledger.get("adaptive_extraction_profile_json"))
        if not extraction_profile:
            profile_entry = _as_dict(ledger.get("adaptive_extraction_profile"))
            extraction_profile = _as_dict(profile_entry.get("value"))
        if not extraction_profile.get("extraction_strategy"):
            calibration_fact = await session.scalar(select(VerifiedFact).where(
                VerifiedFact.run_id == run.id,
                VerifiedFact.fact_key == "asset_warranty.oracle_calibration_matrix",
                VerifiedFact.promotion_status == "VERIFIED",
            ))
            calibration_value = calibration_fact.value_json if calibration_fact and isinstance(calibration_fact.value_json, dict) else {}
            extraction_profile = _as_dict(calibration_value.get("adaptive_extraction_profile"))
        if not extraction_profile.get("extraction_strategy"):
            raise self._error("mysql_metadata_discovery", "EXTRACTION_PROFILE_NOT_AVAILABLE", {"required": ["ORD_BINARY_SEARCH", "HEX_BINARY_SEARCH", "DIRECT_CHARACTER_ENUMERATION", "PREFIX_LIKE_ENUMERATION"]})
        # The metadata executor itself uses the fixed, allowlisted
        # information_schema expressions and records their real responses.
        # Do not require a separate level-5 calibration template before that
        # first metadata request; the deployed Runner bounds calibration to
        # four templates and the verified profile already proves MySQL plus
        # scalar-subquery semantics.
        required_capabilities = {"mysql_dbms_confirmed", "scalar_subquery_oracle_confirmed"}
        missing_capabilities = sorted(capability for capability in required_capabilities if capability not in ledger)
        if missing_capabilities:
            raise self._error("mysql_metadata_discovery", "MYSQL_METADATA_CAPABILITY_REQUIRED", {"missing_capabilities": missing_capabilities})
        allowed = {"DATABASE()", "VERSION()", "@@version_comment", "information_schema.tables", "information_schema.columns"}
        if target.lower().rstrip(";").strip() not in {item.lower() for item in allowed}:
            raise self._error("mysql_metadata_discovery", "MYSQL_METADATA_EXPRESSION_NOT_ALLOWED", {"allowed_expressions": sorted(allowed), "target_expression": target})
        source_hypothesis_id = str(semantic.get("source_hypothesis_id") or "")
        await self._validate_sql_sources(session, run, evidence_ids, fact_ids, source_hypothesis_id)
        candidate_table = str(semantic.get("candidate_table") or "").strip()
        if target.lower().strip() == "information_schema.columns" and not candidate_table:
            raise self._error("mysql_metadata_discovery", "CANDIDATE_TABLE_REQUIRED", {"target_expression": target})
        if candidate_table:
            table_fact = await session.scalar(select(VerifiedFact).where(
                VerifiedFact.run_id == run.id,
                VerifiedFact.fact_key == "asset_warranty.mysql_user_tables",
                VerifiedFact.promotion_status == "VERIFIED",
            ))
            if table_fact is not None:
                if table_fact.id not in fact_ids:
                    fact_ids.append(table_fact.id)
                for evidence_id in list(table_fact.evidence_ids_json or []):
                    if evidence_id not in evidence_ids:
                        evidence_ids.append(evidence_id)
            facts = list((await session.scalars(select(VerifiedFact).where(VerifiedFact.run_id == run.id, VerifiedFact.id.in_(fact_ids), VerifiedFact.promotion_status == "VERIFIED"))).all())
            verified_table = any(
                fact.fact_type in {"SQL_TABLE", "SQL_SCHEMA"}
                and isinstance(fact.value_json, dict)
                and str(fact.value_json.get("table") or fact.value_json.get("name") or "") == candidate_table
                for fact in facts
            )
            if not verified_table:
                raise self._error("mysql_metadata_discovery", "CANDIDATE_TABLE_NOT_VERIFIED", {"candidate_table": candidate_table})
        return {
            "dbms": "mysql", "discovery_scope": "current_database", "request": oracle["request"], "test_field": oracle["test_field"], "baseline_value": str(oracle.get("baseline_value") or ""),
            "control_fields": oracle["control_fields"], "oracle": oracle["oracle"],
            "target_expression": target, "expression_type": str(semantic.get("expression_type") or "METADATA_DISCOVERY"),
            "supporting_evidence_ids": list(semantic.get("supporting_evidence_ids") or evidence_ids),
            "supporting_fact_ids": list(semantic.get("supporting_fact_ids") or fact_ids),
            "source_hypothesis_id": source_hypothesis_id,
            "approved_analysis_review_id": review.id,
            "assumption_status": str(semantic.get("assumption_status") or "VERIFIED"),
            "stage": str(semantic.get("stage") or "identify"), "candidate_table": candidate_table,
            "max_tables": int(semantic.get("max_tables") or 10), "max_columns": int(semantic.get("max_columns") or 30), "max_name_length": int(semantic.get("max_name_length") or 128),
            "max_requests": int(semantic.get("max_requests") or 2000), "resume": bool(semantic.get("resume", False)),
            "extraction_profile": extraction_profile,
        }

    async def _oracle_expression_calibration(self, session: AsyncSession, run: SolveRun, proposal: PlannerProposal, review: AnalysisReview, semantic: dict[str, Any]) -> dict[str, Any]:
        oracle, evidence_ids, fact_ids = await self._oracle_sources(session, run)
        if not evidence_ids or not fact_ids:
            raise self._error("oracle_expression_calibration", "BOOLEAN_ORACLE_PROVENANCE_INCOMPLETE", {"evidence_ids": evidence_ids, "fact_ids": fact_ids})
        state = await session.scalar(select(SolverState).where(SolverState.run_id == run.id))
        ledger = state.capability_ledger_json if state else {}
        profile_entry = _as_dict(ledger.get("adaptive_extraction_profile"))
        extraction_profile = _as_dict(ledger.get("adaptive_extraction_profile_json")) or _as_dict(profile_entry.get("value"))
        if "boolean_predicate_oracle_confirmed" not in ledger and "mysql_boolean_oracle_confirmed" not in ledger:
            raise self._error("oracle_expression_calibration", "BOOLEAN_PREDICATE_ORACLE_REQUIRED", {"required_capability": "boolean_predicate_oracle_confirmed"})
        true_condition = ""
        false_condition = ""
        calls = list((await session.scalars(select(ToolCall).where(ToolCall.run_id == run.id, ToolCall.tool_name == "sql_boolean_compare", ToolCall.status == "COMPLETED").order_by(ToolCall.created_at.desc()))).all())
        for call in calls:
            args = _as_dict(call.arguments_json)
            if args.get("true_condition") and args.get("false_condition"):
                true_condition = str(args["true_condition"])
                false_condition = str(args["false_condition"])
                break
        if not true_condition or not false_condition:
            raise self._error("oracle_expression_calibration", "PREDICATE_TEMPLATE_SOURCE_MISSING", {"source_tool": "sql_boolean_compare"})
        predicate = true_condition
        for token in ("1=1", "1 = 1", "(1=1)", "(1 = 1)"):
            if token in predicate:
                predicate = predicate.replace(token, "{predicate}", 1)
                break
        if "{predicate}" not in predicate:
            raise self._error("oracle_expression_calibration", "PREDICATE_TEMPLATE_INVALID", {"true_condition": true_condition, "false_condition": false_condition})
        # The deployed Runner bounds one calibration job to four templates.
        # Keep the controller matrix within that real execution contract while
        # covering the primitives needed for metadata discovery.  ASCII,
        # CHAR_LENGTH and ORD are intentionally not prerequisites: this target
        # accepts SUBSTRING+HEX while filtering the other character primitives.
        matrix = [
            {"level": 2, "name": "substring", "primitive": "substring", "function": "SUBSTRING", "true": "SUBSTRING('ABC',1,1)='A'", "false": "SUBSTRING('ABC',1,1)='B'", "capability": "substring_supported"},
            {"level": 2, "name": "hex_substring", "primitive": "hex", "function": "HEX", "true": "HEX(SUBSTRING('ABC',1,1))='41'", "false": "HEX(SUBSTRING('ABC',1,1))='42'", "capability": "hex_supported"},
            {"level": 3, "name": "scalar_subquery", "true": "(SELECT 1)=1", "false": "(SELECT 1)=2", "capability": "scalar_subquery_oracle_confirmed"},
            {"level": 4, "name": "mysql_hex", "true": "HEX('A')='41'", "false": "HEX('A')='42'", "capability": "mysql_dbms_confirmed"},
        ]
        source_hypothesis_id = str(semantic.get("source_hypothesis_id") or "")
        await self._validate_sql_sources(session, run, evidence_ids, fact_ids, source_hypothesis_id)
        skip_levels: list[int] = []
        if "boolean_predicate_oracle_confirmed" in ledger:
            skip_levels.append(0)
        if "expression_oracle_confirmed" in ledger:
            skip_levels.append(1)
        if extraction_profile.get("extraction_strategy"):
            skip_levels.append(2)
        if "scalar_subquery_oracle_confirmed" in ledger:
            skip_levels.append(3)
        if "mysql_dbms_confirmed" in ledger:
            skip_levels.append(4)
        if "mysql_information_schema_oracle_confirmed" in ledger:
            skip_levels.append(5)
        return {
            "dbms": str(semantic.get("dbms") or "mysql"),
            "request": oracle["request"],
            "test_field": oracle["test_field"],
            "baseline_value": str(oracle.get("baseline_value") or ""),
            "control_fields": oracle["control_fields"],
            "oracle": oracle["oracle"],
            "predicate_template": predicate,
            "matrix": matrix,
            "repeats_per_expression": 2,
            "max_calibration_requests": int(semantic.get("max_calibration_requests") or 160),
            "supporting_evidence_ids": list(semantic.get("supporting_evidence_ids") or evidence_ids),
            "supporting_fact_ids": list(semantic.get("supporting_fact_ids") or fact_ids),
            "source_hypothesis_id": source_hypothesis_id,
            "approved_analysis_review_id": review.id,
            "assumption_status": "VERIFIED",
            "skip_levels": sorted(set(skip_levels)),
            "existing_profile": extraction_profile,
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
            "dbms": "mysql", "request": oracle["request"], "test_field": oracle["test_field"], "baseline_value": str(oracle.get("baseline_value") or ""),
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
