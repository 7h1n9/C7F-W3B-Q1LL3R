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

from sqlalchemy import func, select

from app.challenge_adapters import adapter_for
from app.core.exceptions import DomainError
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

    async def _new_turn(self, session, run: SolveRun, task: AgentTask, prompt: str) -> str:
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
        turn_id = str(turn.id)
        await session.commit()
        # Only the scalar ID crosses the commit/runtime boundary.  Callers
        # reload the turn in _finish_turn instead of retaining an expired ORM
        # instance.
        return turn_id

    async def _finish_turn(self, session, run: SolveRun, task: AgentTask, turn_id: str, trace: dict[str, Any], action: dict[str, Any], *, parse_error: str | None = None) -> None:
        turn = await session.get(AgentTurn, turn_id)
        if turn is None:
            raise DomainError("AGENT_TURN_NOT_FOUND", "Agent turn disappeared before completion.", {"turn_id": turn_id})
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
        task_context = task.context_json or {}
        context = {
            "run_id": task.run_id, "agent_task_id": task.id, "role": task.agent_role,
            "task_kind": task.task_kind, "objective": task.objective,
            "success_condition": task.success_condition,
            "stop_conditions": task.stop_conditions_json or [],
            "allowed_tools": task.allowed_tools_json or [],
            "current_phase": task_context.get("current_phase"),
            "task_policy": task_context.get("task_policy") or {},
            "task_context": task_context,
            "memory": memory,
            "challenge": {"name": challenge.name, "description": challenge.description, "target_url": challenge.target_url, "allowed_hosts": challenge.allowed_hosts, "metadata": challenge.metadata_json or {}},
            "challenge_adapter": adapter.context(challenge) if adapter else None,
        }
        if task.agent_role == AgentRole.PLANNER.value:
            schema = {"proposal": PlannerProposalContract.model_json_schema()}
            instruction = "Output only PlannerProposalContract, either as the object itself or wrapped in {proposal: ...}. Do not output AgentTaskResult, status, new_facts, or proposed_next_action. allowed_tools must contain only exact names from the Controller catalog: http_request, content_discovery, sql_boolean_compare, oracle_probe_matrix, mysql_metadata_discovery, boolean_config_extract, script_run, http_compare."
            required_strategy = str((memory or {}).get("next_required_strategy") or "").upper()
            attack_state = (memory or {}).get("attack_state") or {}
            attack_actions = list(attack_state.get("available_actions") or (memory or {}).get("available_actions") or []) if isinstance(attack_state, dict) else list((memory or {}).get("available_actions") or [])
            if attack_actions:
                instruction += f" AttackState is the hard action-space boundary. You may select only one of available_actions={attack_actions!r}; do not invent a family, reuse a blocked strategy, or infer a different transition."
            if required_strategy:
                if required_strategy.startswith("BOOLEAN_"):
                    strategy_family = "BOOLEAN"
                    strategy_variant = required_strategy.removeprefix("BOOLEAN_")
                else:
                    strategy_family = required_strategy
                    strategy_variant = required_strategy
                instruction += (
                    f" The Strategy Portfolio is a hard constraint. You MUST set "
                    f"strategy_family={strategy_family!r} and "
                    f"strategy_variant={strategy_variant!r}; their canonical identity "
                    f"must be exactly {required_strategy}. Do not omit these fields, "
                    "put endpoint/condition words in them, choose another strategy, "
                    "or reuse a tried strategy. The Controller will reject a missing "
                    "or different identity."
                )
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
        raise RuntimeError("ROLE_CONTRACT_ENGINE_REQUIRED")

    async def _role_action(self, engine: object, messages: list[dict[str, Any]]) -> tuple[RoleAction, dict[str, Any]]:
        if isinstance(engine, OpenAICompatibleEngine):
            action = await engine.next_role_action(messages)
            return action, dict(engine.last_trace or {})
        raise RuntimeError("ROLE_ACTION_ENGINE_REQUIRED")

    async def _execution_loop(self, session, run: SolveRun, challenge: Challenge, attempt: RunAttempt, task: AgentTask, memory: dict, lease_token: str, prompt: str) -> tuple[AgentTaskResultContract, dict[str, Any]]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": prompt}]
        trace: dict[str, Any] = {}
        continuation = False
        engine = self.engine
        max_turns = max(1, int((task.budget_json or {}).get("max_internal_requests", 1)))
        used_calls = 0
        last_tool_result: dict[str, Any] | None = None
        for _ in range(max_turns):
            turn_prompt = prompt if not continuation else "Tool execution is complete for this step. Review the compact result below and output exactly one next RoleAction."
            if continuation:
                messages.append({"role": "user", "content": turn_prompt})
            turn = await self._new_turn(session, run, task, json.dumps(messages, ensure_ascii=False, default=str))
            try:
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
                repaired, repair_trace = await self._role_action(engine, [{"role": "system", "content": repair_prompt}])
                if not isinstance(repaired, RoleFinishAction):
                    raise ValueError("finalizer repair returned a tool action")
                await self._finish_turn(session, run, task, repair_turn, repair_trace, repaired.model_dump(mode="json"))
                return repaired.result, repair_trace
            except Exception as second:
                await self._finish_turn(session, run, task, repair_turn, {"response_excerpt": str(second)}, {}, parse_error="FINALIZER_SCHEMA_INVALID")
                return self._failure(task.id, "FINALIZER_SCHEMA_INVALID", "Finalizer remained invalid after one schema repair.", status=AgentTaskStatus.PARTIAL), {"parse_error_code": "FINALIZER_SCHEMA_INVALID", "repair_error": str(second)[:1000]}

    async def execute(
        self,
        session,
        run_id: str,
        challenge_id: str,
        attempt_id: str,
        task_id: str,
        lease_token: str,
    ) -> AgentTaskResultContract:
        # Runtime execution starts at a transaction boundary.  Do not accept
        # ORM instances from the orchestrator: commits may expire them and a
        # later attribute access would try to lazy-load outside greenlet_spawn.
        run = await session.get(SolveRun, run_id)
        challenge = await session.get(Challenge, challenge_id)
        attempt = await session.get(RunAttempt, attempt_id)
        task = await session.get(AgentTask, task_id)
        if not all((run, challenge, attempt, task)):
            raise DomainError(
                "ROLE_RUNTIME_CONTEXT_MISSING",
                "Role runtime context could not be reloaded from durable IDs.",
                {"run_id": run_id, "challenge_id": challenge_id, "attempt_id": attempt_id, "task_id": task_id},
            )
        policy = await self._policy(session, task)
        memory = await deterministic_controller.memory.read_for_role(session, run.id, task.agent_role)
        prompt = self._prompt(task, policy, memory, challenge)
        await deterministic_controller.touch_task(session, task.id)
        if run.engine_type == "openai_compatible":
            if task.agent_role in {AgentRole.PLANNER.value, AgentRole.ANALYSIS.value}:
                raise RuntimeError("OPENAI_ROLE_CONTRACT_NOT_CONFIGURED")
            return (await self._execution_loop(session, run, challenge, attempt, task, memory, lease_token, prompt))[0]
        raise RuntimeError(f"unsupported engine type for role runtime: {run.engine_type}")
