"""Controller-owned role execution.

The model is deliberately reduced to a proposer of one structured action. It
never receives MCP tools in this mode and it never owns the tool loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import func, select

from app.challenge_adapters import adapter_for
from app.core.exceptions import DomainError
from app.engines.codex_bridge import CodexSdkEngine
from app.engines.openai_compatible import OpenAICompatibleEngine
from app.models.challenge import Challenge
from app.models.multi_agent import AgentRolePolicy, AgentTask, ApprovedAction
from app.models.run import AgentTurn, RunAttempt, SolveRun
from app.schemas.multi_agent import (
    AgentRole,
    AgentTaskKind,
    AgentTaskResultContract,
    AgentTaskStatus,
    AnalysisReviewContract,
    PlannerProposalContract,
    RoleAction,
    RoleFinishAction,
    TaskBudget,
)
from app.services.multi_agent import deterministic_controller


def _json_object(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text or ""):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _errors(error: Exception) -> str:
    return str(error)[:4000]


class RoleAgentRuntime:
    """Execute Planner/Analysis contracts and one-action role turns."""

    def __init__(self, engine: object | None = None, tool_invoker=None) -> None:
        self.engine = engine
        self.tool_invoker = tool_invoker

    async def _policy(self, session, task: AgentTask) -> AgentRolePolicy:
        policy = await session.scalar(select(AgentRolePolicy).where(AgentRolePolicy.role == task.agent_role, AgentRolePolicy.enabled.is_(True)))
        if policy is None:
            raise RuntimeError(f"AGENT_ROLE_NOT_CONFIGURED:{task.agent_role}")
        return policy

    async def _new_turn(self, session, run: SolveRun, task: AgentTask, prompt: str) -> AgentTurn:
        step = int(await session.scalar(select(func.max(AgentTurn.step_number)).where(AgentTurn.run_id == run.id)) or 0) + 1
        turn = AgentTurn(
            run_id=run.id, agent_task_id=task.id, agent_role=task.agent_role,
            step_number=step, model_config_id=run.model_config_id,
            action_protocol="role_action" if task.agent_role in {AgentRole.RECON.value, AgentRole.EXPLOIT.value, AgentRole.VERIFY.value} else "role_contract",
            prompt_hash=hashlib.sha256(prompt.encode("utf-8", "replace")).hexdigest(),
            context_size_chars=len(prompt), turn_started_at=datetime.now(UTC),
            action_json={"task_kind": task.task_kind, "role": task.agent_role},
        )
        session.add(turn)
        await session.flush()
        run.active_turn_id = turn.id
        await deterministic_controller.touch_task(session, task.id)
        await session.commit()
        return turn

    async def _finish_turn(self, session, run: SolveRun, task: AgentTask, turn: AgentTurn, trace: dict[str, Any], action: dict[str, Any], *, parse_error: str | None = None) -> None:
        turn.latency_ms = trace.get("latency_ms")
        turn.input_tokens = trace.get("input_tokens")
        turn.output_tokens = trace.get("output_tokens")
        turn.provider_request_id = trace.get("provider_request_id") or trace.get("thread_id")
        turn.parse_attempts = int(trace.get("parse_attempts") or 1)
        turn.parse_error_code = parse_error or trace.get("parse_error_code")
        turn.response_excerpt_redacted = str(trace.get("response_excerpt") or trace.get("message") or "")[:2000]
        turn.action_json = action
        turn.turn_finished_at = datetime.now(UTC)
        if run.active_turn_id == turn.id:
            run.active_turn_id = None
        await deterministic_controller.touch_task(session, task.id)
        run.run_total_agent_steps = int(run.run_total_agent_steps or 0) + 1
        run.attempt_agent_steps = int(run.attempt_agent_steps or 0) + 1
        run.agent_step_count = int(run.agent_step_count or 0) + 1
        attempt = await session.scalar(select(RunAttempt).where(RunAttempt.run_id == run.id).order_by(RunAttempt.created_at.desc()))
        if attempt is not None:
            attempt.agent_steps = int(attempt.agent_steps or 0) + 1
            attempt.attempt_agent_steps = int(attempt.attempt_agent_steps or 0) + 1
        await session.commit()

    def _prompt(self, task: AgentTask, policy: AgentRolePolicy, memory: dict, challenge: Challenge) -> str:
        adapter = adapter_for(challenge)
        context = {
            "run_id": task.run_id, "agent_task_id": task.id, "role": task.agent_role,
            "task_kind": task.task_kind, "objective": task.objective,
            "success_condition": task.success_condition,
            "stop_conditions": task.stop_conditions_json or [],
            "allowed_tools": task.allowed_tools_json or [], "task_context": task.context_json or {},
            "memory": memory,
            "challenge": {"name": challenge.name, "description": challenge.description, "target_url": challenge.target_url, "allowed_hosts": challenge.allowed_hosts, "metadata": challenge.metadata_json or {}},
            "challenge_adapter": adapter.context(challenge) if adapter else None,
        }
        if task.agent_role == AgentRole.PLANNER.value:
            schema = {"proposal": PlannerProposalContract.model_json_schema()}
            instruction = "Output only PlannerProposalContract, either as the object itself or wrapped in {proposal: ...}. Do not output AgentTaskResult, status, new_facts, or proposed_next_action. allowed_tools must contain only exact names from the Controller catalog: http_request, content_discovery, sql_boolean_compare, oracle_probe_matrix, sqlite_metadata_discovery, boolean_config_extract, script_run, http_compare."
            if adapter:
                instruction += " For the asset_warranty adapter, use only http_request for RECON proposals. Schedule exactly one bounded request per proposal; never use http_compare, never put a requests array in approved_arguments, and do not combine valid and invalid controls under max_logical_calls=1. Read the endpoint, method, fields, and control values from challenge_adapter."
        elif task.agent_role == AgentRole.ANALYSIS.value:
            schema = {"review": AnalysisReviewContract.model_json_schema()}
            instruction = "Output only AnalysisReviewContract, either as the object itself or wrapped in {review: ...}. task_kind must be PLAN_REVIEW or RESULT_REVIEW."
        else:
            schema = {"one_of": {"tool": {"type": "tool", "tool_name": "string", "arguments": "object", "purpose": "string", "expected_signal": "object", "stop_if": ["string"]}, "finish": {"type": "finish", "result": "AgentTaskResultContract"}}}
            instruction = "Output exactly one RoleAction. A tool action is one tool request only; a finish action must contain the complete AgentTaskResultContract. Never call MCP, never emit multiple actions, and never do another role's work."
            if adapter and task.agent_role == AgentRole.RECON.value:
                instruction += " For the asset_warranty adapter, an http_request arguments object MUST use method, url, and a string body (JSON-encode the documented fields); never emit a requests array or multiple requests. A single logical call is one HTTP request. If the objective needs a valid-vs-invalid comparison, emit one valid request now and finish with its evidence so the Planner can schedule the invalid control as a separate bounded proposal."
        return (
            f"{policy.system_prompt}\nYou are executing a bounded {task.agent_role} role task, not the whole CTF. "
            "The Controller owns tools, evidence, facts, capabilities, leases, and lifecycle state. "
            "Each model turn has one action. When the success condition, stop condition, or budget is met, output FinishAction.\n"
            f"{instruction}\nSCHEMA={json.dumps(schema, ensure_ascii=False, default=str)}\nCONTEXT={json.dumps(context, ensure_ascii=False, default=str)}"
        )

    @staticmethod
    def _baseline_request(challenge: Challenge, task: AgentTask) -> dict[str, Any]:
        metadata = challenge.metadata_json or {}
        template = metadata.get("baseline_request") or metadata.get("request") or {}
        if not isinstance(template, dict):
            template = {}
        return {
            "url": str(template.get("url") or challenge.target_url or ""),
            "method": str(template.get("method") or "GET").upper(),
            **({"headers": template["headers"]} if isinstance(template.get("headers"), dict) else {}),
            **({"params": template["params"]} if isinstance(template.get("params"), dict) else {}),
            **({"body": template["body"]} if isinstance(template.get("body"), (dict, str)) else {}),
            "final_verification": task.agent_role == AgentRole.VERIFY.value,
        }

    @staticmethod
    def _failure(task_id: str, classification: str, reason: str, *, status: AgentTaskStatus = AgentTaskStatus.FAILED) -> AgentTaskResultContract:
        return AgentTaskResultContract(
            task_id=task_id, status=status,
            failure_classification={"fingerprint": classification.lower(), "classification": classification, "retryable": classification != "MODEL_OUTPUT_SCHEMA_INVALID", "reason": reason, "next_allowed_condition": "repair the structured output or create a fresh bounded task"},
            handoff_summary=reason,
        )

    async def _mock(self, session, run: SolveRun, challenge: Challenge, attempt: RunAttempt, task: AgentTask, memory: dict, lease_token: str) -> tuple[AgentTaskResultContract, dict]:
        if task.agent_role == AgentRole.PLANNER.value:
            proposal = PlannerProposalContract(
                proposal_id=f"PP-{uuid.uuid4().hex[:12]}", run_id=run.id,
                current_stage=str(memory.get("stage") or "INTAKE"),
                decision_question="What single bounded observation reduces the active uncertainty?",
                next_agent=AgentRole.RECON if not memory.get("evidence_ids") else AgentRole.EXPLOIT,
                objective="Discover the authorized HTTP surface." if not memory.get("evidence_ids") else "Execute the approved bounded experiment.",
                input_fact_ids=list(memory.get("verified_fact_ids") or []),
                allowed_tools=["http_request"], budget=TaskBudget(max_logical_calls=1, max_internal_requests=4, max_runtime_seconds=300),
                success_condition="produce an evidence-backed handoff", stop_conditions=["stop after the approved experiment"],
            )
            return AgentTaskResultContract(task_id=task.id, status=AgentTaskStatus.COMPLETED, proposed_next_action={"proposal": proposal.model_dump(mode="json")}, handoff_summary="Mock Planner emitted a valid proposal."), {"provider_request_id": f"mock:{task.id}", "action": proposal.model_dump(mode="json")}
        if task.task_kind in {AgentTaskKind.PLAN_REVIEW.value, AgentTaskKind.RESULT_REVIEW.value}:
            proposal = (task.context_json or {}).get("proposal") or {}
            candidates = list((task.context_json or {}).get("candidate_facts") or [])
            review = AnalysisReviewContract(proposal_id=str(proposal.get("proposal_id") or ""), task_kind=task.task_kind, decision="APPROVE", confidence=90, question_being_tested=str(proposal.get("decision_question") or "bounded question"), independent_variable="request_arguments", required_controls={"fresh_request": True}, expected_true_signal={"new_artifact": True}, expected_false_signal={"different_response": True}, recommended_tool=(proposal.get("allowed_tools") or [None])[0], reason="Mock review approved the declared bounded action.", audit_reason="mock", approved_arguments=(task.context_json or {}).get("approved_arguments") or {}, approved_fact_indexes=list(range(len(candidates))) if task.task_kind == AgentTaskKind.RESULT_REVIEW.value and candidates else [], capabilities_added=["request_contract_confirmed"] if task.task_kind == AgentTaskKind.RESULT_REVIEW.value and not candidates else [], solution_step_accepted=task.task_kind == AgentTaskKind.RESULT_REVIEW.value and not candidates, next_phase="MAPPING")
            return AgentTaskResultContract(task_id=task.id, status=AgentTaskStatus.COMPLETED, proposed_next_action={"review": review.model_dump(mode="json")}, handoff_summary="Mock Analysis emitted a valid review."), {"provider_request_id": f"mock:{task.id}", "action": review.model_dump(mode="json")}
        # Mock still follows the controller loop shape: one tool followed by a finish.
        if self.tool_invoker is None:
            raise RuntimeError("ROLE_RUNTIME_TOOL_INVOKER_REQUIRED")
        tool_name = str((task.context_json or {}).get("tool") or (task.allowed_tools_json or ["http_request"])[0])
        approved = await session.get(ApprovedAction, str((task.context_json or {}).get("approved_action_id") or ""))
        if approved is None or approved.compile_status != "COMPILED" or not approved.compiled_arguments_json:
            raise DomainError("APPROVED_ACTION_NOT_COMPILED", "Production task has no compiled ApprovedAction.")
        tool_name = approved.tool_name
        result = await self.tool_invoker(session, run, challenge, tool_name, dict(approved.compiled_arguments_json), execution_layer="multi_agent", logical_tool_call_id=f"mcp:{run.id}:{attempt.id}:agent-task:{task.id}:{uuid.uuid4().hex[:8]}", agent_task_id=task.id, agent_role=task.agent_role, task_lease_token=lease_token, approved_action_id=approved.id)
        finish = AgentTaskResultContract(task_id=task.id, status=AgentTaskStatus.COMPLETED if str(result.get("status") or "").upper() == "COMPLETED" else AgentTaskStatus.PARTIAL, evidence_ids=[], handoff_summary="Mock role finished after the controller executed one action.")
        if finish.status == AgentTaskStatus.PARTIAL:
            finish = finish.model_copy(update={"failure_classification": {"fingerprint": "mock-tool-failure", "classification": "TOOL_FAILURE", "retryable": True, "reason": str(result.get("error") or result.get("summary") or "tool failed"), "next_allowed_condition": "replan"}})
        return finish, {"provider_request_id": f"mock:{task.id}", "action": {"type": "finish", "result": finish.model_dump(mode="json")}}

    async def _bridge_turn(self, engine: CodexSdkEngine, run_id: str, prompt: str, *, continuation: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
        messages: list[str] = []
        trace: dict[str, Any] = {}
        stream = engine.continue_run(run_id, prompt) if continuation else engine.start(run_id, prompt)
        async for event in stream:
            payload = event.payload or {}
            if event.event_type == "agent.message" and payload.get("message"):
                messages.append(str(payload["message"]))
            if event.event_type == "agent.turn_completed":
                trace.update(payload.get("usage") or {})
            trace["thread_id"] = payload.get("thread_id") or trace.get("thread_id")
        return (_json_object(messages[-1] if messages else "") or {}), {**trace, "message": messages[-1] if messages else ""}

    async def _contract_runtime(self, session, run: SolveRun, task: AgentTask, prompt: str) -> tuple[AgentTaskResultContract, dict[str, Any]]:
        if isinstance(self.engine, OpenAICompatibleEngine):
            schema = PlannerProposalContract.model_json_schema() if task.agent_role == AgentRole.PLANNER.value else AnalysisReviewContract.model_json_schema()
            key = "proposal" if task.agent_role == AgentRole.PLANNER.value else "review"
            raw = await self.engine.next_contract([{"role": "system", "content": prompt}], schema, name=f"{task.agent_role.lower()}_{task.task_kind.lower()}")
            candidate = raw.get(key) or raw
            try:
                value = (PlannerProposalContract if key == "proposal" else AnalysisReviewContract).model_validate(candidate)
                return AgentTaskResultContract(task_id=task.id, status=AgentTaskStatus.COMPLETED, proposed_next_action={key: value.model_dump(mode="json")}, handoff_summary=f"{task.agent_role} contract validated."), dict(self.engine.last_trace or {})
            except Exception as error:
                repair_prompt = f"The previous output was invalid for {task.agent_role} contract. Field errors:\n{_errors(error)}\nOutput only corrected JSON."
                repair_turn = await self._new_turn(session, run, task, repair_prompt)
                repaired = await self.engine.next_contract([{"role": "system", "content": repair_prompt}], schema, name=f"{task.agent_role.lower()}_{task.task_kind.lower()}_repair")
                candidate = repaired.get(key) or repaired
                try:
                    value = (PlannerProposalContract if key == "proposal" else AnalysisReviewContract).model_validate(candidate)
                    await self._finish_turn(session, run, task, repair_turn, dict(self.engine.last_trace or {}), {key: value.model_dump(mode="json")})
                    return AgentTaskResultContract(task_id=task.id, status=AgentTaskStatus.COMPLETED, proposed_next_action={key: value.model_dump(mode="json")}, handoff_summary=f"{task.agent_role} contract validated after one repair."), dict(self.engine.last_trace or {})
                except Exception as second:
                    await self._finish_turn(session, run, task, repair_turn, dict(self.engine.last_trace or {}), {"raw": repaired}, parse_error="MODEL_OUTPUT_SCHEMA_INVALID")
                    return self._failure(task.id, "MODEL_OUTPUT_SCHEMA_INVALID", f"Role contract remained invalid after repair: {_errors(second)}"), {"parse_error_code": "MODEL_OUTPUT_SCHEMA_INVALID", "parse_attempts": 2}
        if not isinstance(self.engine, CodexSdkEngine):
            raise RuntimeError("ROLE_CONTRACT_ENGINE_REQUIRED")
        scope = dict(self.engine.scope)
        scope.update({"execution_mode": "controller_tool_loop", "agent_task_id": task.id, "agent_role": task.agent_role, "allowed_tools": [], "task_lease_token": task.lease_token})
        engine = CodexSdkEngine(self.engine.bridge_url, self.engine.workspace_path, scope=scope)
        raw, trace = await self._bridge_turn(engine, run.id, prompt)
        if task.agent_role == AgentRole.PLANNER.value:
            candidate = raw.get("proposal") or raw
            try:
                value = PlannerProposalContract.model_validate(candidate)
                return AgentTaskResultContract(task_id=task.id, status=AgentTaskStatus.COMPLETED, proposed_next_action={"proposal": value.model_dump(mode="json")}, handoff_summary="PlannerProposalContract validated."), trace
            except Exception as error:
                repair_prompt = f"Your previous output did not satisfy PlannerProposalContract. Field errors:\n{_errors(error)}\nOutput only corrected JSON; no explanation or Markdown."
                repair_turn = await self._new_turn(session, run, task, repair_prompt)
                repaired, repair_trace = await self._bridge_turn(engine, run.id, repair_prompt, continuation=True)
                candidate = repaired.get("proposal") or repaired
                try:
                    value = PlannerProposalContract.model_validate(candidate)
                    repair_trace["parse_attempts"] = 2
                    return AgentTaskResultContract(task_id=task.id, status=AgentTaskStatus.COMPLETED, proposed_next_action={"proposal": value.model_dump(mode="json")}, handoff_summary="PlannerProposalContract validated after one repair."), repair_trace
                except Exception as second:
                    await self._finish_turn(session, run, task, repair_turn, repair_trace, {"raw": repaired}, parse_error="MODEL_OUTPUT_SCHEMA_INVALID")
                    return self._failure(task.id, "MODEL_OUTPUT_SCHEMA_INVALID", f"Planner output remained invalid after one repair: {_errors(second)}"), {**trace, "parse_attempts": 2, "parse_error_code": "MODEL_OUTPUT_SCHEMA_INVALID"}
        candidate = raw.get("review") or raw
        try:
            value = AnalysisReviewContract.model_validate(candidate)
            return AgentTaskResultContract(task_id=task.id, status=AgentTaskStatus.COMPLETED, proposed_next_action={"review": value.model_dump(mode="json")}, handoff_summary="AnalysisReviewContract validated."), trace
        except Exception as error:
            repair_prompt = f"Your previous output did not satisfy AnalysisReviewContract. Field errors:\n{_errors(error)}\nOutput only corrected JSON; no explanation or Markdown."
            repair_turn = await self._new_turn(session, run, task, repair_prompt)
            repaired, repair_trace = await self._bridge_turn(engine, run.id, repair_prompt, continuation=True)
            candidate = repaired.get("review") or repaired
            try:
                value = AnalysisReviewContract.model_validate(candidate)
                repair_trace["parse_attempts"] = 2
                return AgentTaskResultContract(task_id=task.id, status=AgentTaskStatus.COMPLETED, proposed_next_action={"review": value.model_dump(mode="json")}, handoff_summary="AnalysisReviewContract validated after one repair."), repair_trace
            except Exception as second:
                await self._finish_turn(session, run, task, repair_turn, repair_trace, {"raw": repaired}, parse_error="MODEL_OUTPUT_SCHEMA_INVALID")
                return self._failure(task.id, "MODEL_OUTPUT_SCHEMA_INVALID", f"Analysis output remained invalid after one repair: {_errors(second)}"), {**trace, "parse_attempts": 2, "parse_error_code": "MODEL_OUTPUT_SCHEMA_INVALID"}

    async def _role_action(self, engine: object, messages: list[dict[str, Any]]) -> tuple[RoleAction, dict[str, Any]]:
        if isinstance(engine, OpenAICompatibleEngine):
            action = await engine.next_role_action(messages)
            return action, dict(engine.last_trace or {})
        raise RuntimeError("ROLE_ACTION_ENGINE_REQUIRED")

    async def _codex_action(self, engine: CodexSdkEngine, run_id: str, prompt: str, *, continuation: bool) -> tuple[RoleAction, dict[str, Any]]:
        raw, trace = await self._bridge_turn(engine, run_id, prompt, continuation=continuation)
        return TypeAdapter(RoleAction).validate_python(raw), trace

    async def _execution_loop(self, session, run: SolveRun, challenge: Challenge, attempt: RunAttempt, task: AgentTask, memory: dict, lease_token: str, prompt: str) -> tuple[AgentTaskResultContract, dict[str, Any]]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": prompt}]
        trace: dict[str, Any] = {}
        continuation = False
        engine = self.engine
        if isinstance(engine, CodexSdkEngine):
            scope = dict(engine.scope)
            scope.update({"execution_mode": "controller_tool_loop", "agent_task_id": task.id, "agent_role": task.agent_role, "allowed_tools": [], "task_lease_token": lease_token})
            engine = CodexSdkEngine(engine.bridge_url, engine.workspace_path, scope=scope)
        max_turns = max(1, int((task.budget_json or {}).get("max_internal_requests", 1)))
        used_calls = 0
        last_tool_result: dict[str, Any] | None = None
        for _ in range(max_turns):
            turn_prompt = prompt if not continuation else "Tool execution is complete for this step. Review the compact result below and output exactly one next RoleAction."
            if continuation:
                messages.append({"role": "user", "content": turn_prompt})
            turn = await self._new_turn(session, run, task, json.dumps(messages, ensure_ascii=False, default=str))
            try:
                if isinstance(engine, CodexSdkEngine):
                    action, action_trace = await self._codex_action(engine, run.id, turn_prompt + "\n" + json.dumps(messages[-1], ensure_ascii=False, default=str), continuation=continuation)
                else:
                    action, action_trace = await self._role_action(engine, messages)
                trace = action_trace
                await self._finish_turn(session, run, task, turn, trace, action.model_dump(mode="json"))
            except Exception as error:
                await self._finish_turn(session, run, task, turn, {"response_excerpt": str(error)}, {}, parse_error="ROLE_ACTION_SCHEMA_INVALID")
                return self._failure(task.id, "MODEL_OUTPUT_SCHEMA_INVALID", f"RoleAction was invalid: {_errors(error)}"), {"parse_error_code": "ROLE_ACTION_SCHEMA_INVALID"}
            if isinstance(action, RoleFinishAction):
                return action.result, trace
            approved_id = (task.context_json or {}).get("approved_action_id")
            approved = await session.get(ApprovedAction, str(approved_id or ""))
            if approved is None or approved.compile_status != "COMPILED" or not approved.compiled_arguments_json:
                raise DomainError("APPROVED_ACTION_NOT_COMPILED", "Production task has no compiled ApprovedAction.")
            if action.tool_name != approved.tool_name or action.tool_name not in (task.allowed_tools_json or []):
                return self._failure(task.id, "ROLE_TOOL_SCOPE_INVALID", f"{action.tool_name} is outside the task contract", status=AgentTaskStatus.NEED_REPLAN), trace
            if self.tool_invoker is None:
                raise RuntimeError("ROLE_RUNTIME_TOOL_INVOKER_REQUIRED")
            # The model's arguments are intentionally ignored.  They are a
            # semantic suggestion, not an executable capability.
            result = await self.tool_invoker(session, run, challenge, approved.tool_name, dict(approved.compiled_arguments_json), execution_layer="multi_agent", logical_tool_call_id=f"mcp:{run.id}:{attempt.id}:agent-task:{task.id}:{uuid.uuid4().hex[:8]}", agent_task_id=task.id, agent_role=task.agent_role, task_lease_token=lease_token, approved_action_id=approved.id)
            last_tool_result = result
            await deterministic_controller.touch_task(session, task.id)
            compact = {"tool": action.tool_name, "status": result.get("status"), "summary": result.get("summary"), "error_code": result.get("error_code"), "model_view": result.get("model_view"), "artifact_id": result.get("artifact_id"), "observation_id": result.get("observation_id")}
            messages.extend([{"role": "assistant", "content": json.dumps(action.model_dump(mode="json"), ensure_ascii=False)}, {"role": "user", "content": json.dumps({"tool_result": compact}, ensure_ascii=False, default=str)}])
            continuation = True
            used_calls += 1
            if used_calls >= int((task.budget_json or {}).get("max_logical_calls", 0)):
                break
        # A one-call atomic task is complete as soon as its ToolCall result is
        # durable.  Controller normalization supplies the handoff; waiting
        # for a model Finalizer must not turn a successful task into PARTIAL.
        if int((task.budget_json or {}).get("max_logical_calls", 0)) <= 1 and used_calls == 1:
            completed = str((last_tool_result or {}).get("status") or "").upper() == "COMPLETED"
            if completed:
                return AgentTaskResultContract(task_id=task.id, status=AgentTaskStatus.COMPLETED, handoff_summary="One approved tool action completed and produced durable evidence."), {"controller_finalized": True}
            return self._failure(task.id, "TOOL_FAILURE", str((last_tool_result or {}).get("error") or (last_tool_result or {}).get("summary") or "The approved tool action failed."), status=AgentTaskStatus.PARTIAL), {"controller_finalized": True}
        # Budget/success boundary: one finalizer turn with no tools in scope.
        finalizer_prompt = "工具执行阶段已经结束。不得再调用任何工具。仅依据以下 Task、Tool Result、Evidence 和 Artifact 摘要输出 RoleFinishAction。\n" + json.dumps(messages[-2:], ensure_ascii=False, default=str)
        final_turn = await self._new_turn(session, run, task, finalizer_prompt)
        try:
            if isinstance(engine, CodexSdkEngine):
                action, final_trace = await self._codex_action(engine, run.id, finalizer_prompt, continuation=True)
            else:
                action, final_trace = await self._role_action(engine, [{"role": "system", "content": finalizer_prompt}])
            if not isinstance(action, RoleFinishAction):
                raise ValueError("finalizer returned a tool action")
            await self._finish_turn(session, run, task, final_turn, final_trace, action.model_dump(mode="json"))
            return action.result, final_trace
        except Exception as error:
            await self._finish_turn(session, run, task, final_turn, {"response_excerpt": str(error)}, {}, parse_error="FINALIZER_SCHEMA_INVALID")
            repair_prompt = finalizer_prompt + "\nThe previous finalizer output was invalid. Output only a valid RoleFinishAction JSON; do not call tools."
            repair_turn = await self._new_turn(session, run, task, repair_prompt)
            try:
                if isinstance(engine, CodexSdkEngine):
                    repaired, repair_trace = await self._codex_action(engine, run.id, repair_prompt, continuation=True)
                else:
                    repaired, repair_trace = await self._role_action(engine, [{"role": "system", "content": repair_prompt}])
                if not isinstance(repaired, RoleFinishAction):
                    raise ValueError("finalizer repair returned a tool action")
                await self._finish_turn(session, run, task, repair_turn, repair_trace, repaired.model_dump(mode="json"))
                return repaired.result, repair_trace
            except Exception as second:
                await self._finish_turn(session, run, task, repair_turn, {"response_excerpt": str(second)}, {}, parse_error="FINALIZER_SCHEMA_INVALID")
                return self._failure(task.id, "FINALIZER_SCHEMA_INVALID", "Finalizer remained invalid after one schema repair.", status=AgentTaskStatus.PARTIAL), {"parse_error_code": "FINALIZER_SCHEMA_INVALID", "repair_error": str(second)[:1000]}

    async def execute(self, session, run: SolveRun, challenge: Challenge, attempt: RunAttempt, task: AgentTask, lease_token: str) -> AgentTaskResultContract:
        policy = await self._policy(session, task)
        memory = await deterministic_controller.memory.read_for_role(session, run.id, task.agent_role)
        prompt = self._prompt(task, policy, memory, challenge)
        await deterministic_controller.touch_task(session, task.id)
        if run.engine_type == "codex_sdk":
            turn = await self._new_turn(session, run, task, prompt)
            try:
                if task.agent_role in {AgentRole.PLANNER.value, AgentRole.ANALYSIS.value}:
                    result, trace = await asyncio.wait_for(self._contract_runtime(session, run, task, prompt), timeout=max(10, int((task.budget_json or {}).get("max_runtime_seconds", 300))))
                else:
                    # The execution loop creates its own AgentTurn rows. Close
                    # the bootstrap marker without treating it as a model turn.
                    await self._finish_turn(session, run, task, turn, {"message": "controller loop started"}, {"execution_mode": "controller_tool_loop"})
                    return (await asyncio.wait_for(self._execution_loop(session, run, challenge, attempt, task, memory, lease_token, prompt), timeout=max(10, int((task.budget_json or {}).get("max_runtime_seconds", 300)))))[0]
            except asyncio.TimeoutError:
                result, trace = self._failure(task.id, "ROLE_IDLE_TIMEOUT", "Role task exceeded its active deadline.", status=AgentTaskStatus.PARTIAL), {"parse_error_code": "ROLE_IDLE_TIMEOUT"}
            await self._finish_turn(session, run, task, turn, trace, result.proposed_next_action)
            return result
        if run.engine_type == "openai_compatible":
            if task.agent_role in {AgentRole.PLANNER.value, AgentRole.ANALYSIS.value}:
                raise RuntimeError("OPENAI_ROLE_CONTRACT_NOT_CONFIGURED")
            return (await self._execution_loop(session, run, challenge, attempt, task, memory, lease_token, prompt))[0]
        turn = await self._new_turn(session, run, task, prompt)
        result, trace = await self._mock(session, run, challenge, attempt, task, memory, lease_token)
        await self._finish_turn(session, run, task, turn, trace, result.proposed_next_action)
        return result
