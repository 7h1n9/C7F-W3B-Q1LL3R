"""Model-backed runtime for the five multi-agent roles.

The controller owns leases and promotion.  This module owns only one thing:
turning a leased task into a bounded model/tool interaction and returning a
validated ``AgentTaskResultContract``.  The mock adapter is deliberately
deterministic and is used by tests only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from app.engines.codex_bridge import CodexSdkEngine
from app.engines.openai_compatible import OpenAICompatibleEngine
from app.models.challenge import Challenge
from app.models.multi_agent import AgentRolePolicy, AgentTask
from app.models.run import AgentTurn, RunAttempt, SolveRun
from app.schemas.multi_agent import (
    AgentRole,
    AgentTaskKind,
    AgentTaskResultContract,
    AgentTaskStatus,
    AnalysisDecision,
    AnalysisReviewContract,
    PlannerProposalContract,
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


class RoleAgentRuntime:
    """Execute one leased role task with model and tool scope enforcement."""

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
            run_id=run.id,
            agent_task_id=task.id,
            agent_role=task.agent_role,
            step_number=step,
            model_config_id=run.model_config_id,
            action_protocol="json_schema" if run.engine_type != "codex_sdk" else "codex_sdk",
            prompt_hash=hashlib.sha256(prompt.encode("utf-8", "replace")).hexdigest(),
            context_size_chars=len(prompt),
            turn_started_at=datetime.now(UTC),
            action_json={"task_kind": task.task_kind, "role": task.agent_role},
        )
        session.add(turn)
        await session.flush()
        run.active_turn_id = turn.id
        await session.commit()
        return turn

    async def _finish_turn(self, session, run: SolveRun, turn: AgentTurn, trace: dict[str, Any], action: dict[str, Any]) -> None:
        turn.latency_ms = trace.get("latency_ms")
        turn.input_tokens = trace.get("input_tokens")
        turn.output_tokens = trace.get("output_tokens")
        turn.provider_request_id = trace.get("provider_request_id") or trace.get("thread_id")
        turn.parse_attempts = int(trace.get("parse_attempts") or 1)
        turn.parse_error_code = trace.get("parse_error_code")
        turn.response_excerpt_redacted = str(trace.get("response_excerpt") or trace.get("message") or "")[:2000]
        turn.action_json = action
        turn.turn_finished_at = datetime.now(UTC)
        if run.active_turn_id == turn.id:
            run.active_turn_id = None
        run.run_total_agent_steps = int(run.run_total_agent_steps or 0) + 1
        run.attempt_agent_steps = int(run.attempt_agent_steps or 0) + 1
        await session.commit()

    def _prompt(self, task: AgentTask, policy: AgentRolePolicy, memory: dict, challenge: Challenge) -> str:
        context = {
            "run_id": task.run_id,
            "agent_task_id": task.id,
            "role": task.agent_role,
            "task_kind": task.task_kind,
            "objective": task.objective,
            "success_condition": task.success_condition,
            "stop_conditions": task.stop_conditions_json or [],
            "allowed_tools": task.allowed_tools_json or [],
            "task_context": task.context_json or {},
            "memory": memory,
            "challenge": {"name": challenge.name, "description": challenge.description, "target_url": challenge.target_url, "allowed_hosts": challenge.allowed_hosts, "metadata": challenge.metadata_json or {}},
        }
        output = {
            "status": "COMPLETED|FAILED|BLOCKED|NEED_REPLAN",
            "new_facts": [], "evidence_ids": [], "updated_hypotheses": [],
            "accepted_solution_steps": [], "rejected_paths": [],
            "failure_classification": None, "proposed_next_action": {}, "handoff_summary": "",
        }
        return (
            f"{policy.system_prompt}\nYou are the {task.agent_role} role in an authorized CTF run. "
            "Use only the declared tools and never change lifecycle state. Every claim must cite durable evidence. "
            "Return one JSON object matching the result envelope below; do not return markdown.\n"
            f"RESULT_ENVELOPE={json.dumps(output, ensure_ascii=False)}\nCONTEXT={json.dumps(context, ensure_ascii=False, default=str)}"
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

    async def _mock(self, session, run: SolveRun, challenge: Challenge, attempt: RunAttempt, task: AgentTask, memory: dict, lease_token: str) -> tuple[AgentTaskResultContract, dict]:
        if task.agent_role == AgentRole.PLANNER.value:
            evidence = list(memory.get("evidence_ids") or [])
            approved = (task.context_json or {}).get("approved_review")
            if memory.get("working_memory", {}).get("candidate_seen"):
                next_agent, tools, objective = AgentRole.VERIFY, ["http_request"], "Freshly reproduce the unverified candidate with an independent request."
            elif approved:
                next_agent, tools, objective = AgentRole.EXPLOIT, list(approved.get("allowed_tools") or ["http_request"]), "Execute the analysis-approved bounded experiment."
            elif evidence:
                next_agent, tools, objective = AgentRole.ANALYSIS, ["http_request"], "Review the proposed experiment against evidence and controls."
            else:
                next_agent, tools, objective = AgentRole.RECON, ["http_request", "content_discovery"], "Establish a fresh authorized baseline and discover one entry surface."
            proposal = PlannerProposalContract(
                proposal_id=f"PP-{uuid.uuid4().hex[:12]}", run_id=run.id, current_stage=str(memory.get("stage") or "INTAKE"),
                decision_question="Which bounded next action most reduces the active uncertainty?", next_agent=next_agent,
                objective=objective, input_fact_ids=list(memory.get("verified_fact_ids") or []), allowed_tools=tools,
                budget=TaskBudget(max_logical_calls=max(1, len(tools)), max_internal_requests=8, max_runtime_seconds=120),
                success_condition="produce a fresh evidence-backed handoff", stop_conditions=["stop after one discriminating result"],
            )
            return AgentTaskResultContract(task_id=task.id, status=AgentTaskStatus.COMPLETED, proposed_next_action={"proposal": proposal.model_dump(mode="json")}, handoff_summary="Mock planner selected the next role from current memory."), {"provider_request_id": f"mock:{task.id}", "input_tokens": 1, "output_tokens": 1, "latency_ms": 0, "action": {"proposal": proposal.model_dump(mode="json")}}
        if task.task_kind in {AgentTaskKind.PLAN_REVIEW.value, AgentTaskKind.RESULT_REVIEW.value}:
            proposal = (task.context_json or {}).get("proposal") or {}
            evidence_ids = list(memory.get("evidence_ids") or [])
            approved = bool(evidence_ids and proposal.get("decision_question") and proposal.get("allowed_tools") and proposal.get("success_condition"))
            review = AnalysisReviewContract(
                proposal_id=str(proposal.get("proposal_id") or ""), task_kind="RESULT_REVIEW" if task.task_kind == AgentTaskKind.RESULT_REVIEW.value else "PLAN_REVIEW",
                decision=AnalysisDecision.APPROVE if approved else AnalysisDecision.NEED_MORE_EVIDENCE,
                confidence=85 if approved else 25, question_being_tested=str(proposal.get("decision_question") or "Can the proposed bounded action discriminate the active hypothesis?"),
                supporting_evidence_ids=evidence_ids, independent_variable="request_parameters",
                required_controls={"authorized_host": "challenge.allowed_hosts", "fresh_request": True},
                expected_true_signal={"new_artifact": True}, expected_false_signal={"no_new_artifact": True},
                recommended_tool=(proposal.get("allowed_tools") or [None])[0],
                reason="Evidence, controls, and success criteria are explicit." if approved else "The proposal lacks an evidence-backed decision basis.",
                audit_reason="mock structured review", approved_arguments=(task.context_json or {}).get("approved_arguments") or {},
            )
            return AgentTaskResultContract(task_id=task.id, status=AgentTaskStatus.COMPLETED, evidence_ids=evidence_ids, proposed_next_action={"review": review.model_dump(mode="json")}, handoff_summary=f"Mock analysis decision: {review.decision.value}."), {"provider_request_id": f"mock:{task.id}", "input_tokens": 1, "output_tokens": 1, "latency_ms": 0, "action": {"review": review.model_dump(mode="json")}}
        if self.tool_invoker is None:
            raise RuntimeError("ROLE_RUNTIME_TOOL_INVOKER_REQUIRED")
        tool_name = str((task.context_json or {}).get("tool") or (task.allowed_tools_json or ["http_request"])[0])
        arguments = dict((task.context_json or {}).get("arguments") or self._baseline_request(challenge, task))
        logical_id = f"mcp:{run.id}:{attempt.id}:agent-task:{task.id}:{uuid.uuid4().hex[:8]}"
        result = await self.tool_invoker(session, run, challenge, tool_name, arguments, execution_layer="multi_agent", logical_tool_call_id=logical_id, agent_task_id=task.id, agent_role=task.agent_role, task_lease_token=lease_token)
        status = AgentTaskStatus.COMPLETED if str(result.get("status") or "").upper() in {"COMPLETED", "SUCCEEDED", "SUCCESS"} else AgentTaskStatus.BLOCKED
        failure = None if status == AgentTaskStatus.COMPLETED else {"fingerprint": f"{task.agent_role.lower()}-tool", "classification": "TOOL_FAILURE", "retryable": True, "reason": str(result.get("error") or result.get("summary") or "tool failed"), "next_allowed_condition": "choose a different bounded action"}
        return AgentTaskResultContract(task_id=task.id, status=status, proposed_next_action={"tool": tool_name, "logical_tool_call_id": logical_id, "result": result}, failure_classification=failure, handoff_summary=f"{task.agent_role} tool loop returned {status.value}."), {"provider_request_id": f"mock:{task.id}", "input_tokens": 1, "output_tokens": 1, "latency_ms": 0, "action": {"tool": tool_name, "result": result}}

    async def _codex(self, session, run: SolveRun, challenge: Challenge, task: AgentTask, memory: dict, lease_token: str, prompt: str) -> tuple[AgentTaskResultContract, dict]:
        base = self.engine
        if not isinstance(base, CodexSdkEngine):
            raise RuntimeError("CODEX_RUNTIME_ENGINE_REQUIRED")
        scope = dict(base.scope)
        scope.update({"agent_task_id": task.id, "agent_role": task.agent_role, "task_lease_token": lease_token, "allowed_tools": list(task.allowed_tools_json or []), "model_turn_id": run.active_turn_id, "turn_id": run.active_turn_id})
        engine = CodexSdkEngine(base.bridge_url, base.workspace_path, scope=scope)
        messages: list[str] = []
        usage: dict[str, Any] = {}
        thread_id = None
        async for event in engine.start(run.id, prompt):
            payload = event.payload or {}
            if event.event_type == "agent.message" and payload.get("message"):
                messages.append(str(payload["message"]))
            if event.event_type == "agent.turn_completed":
                usage = dict(payload.get("usage") or {})
            thread_id = payload.get("thread_id") or thread_id
        raw = _json_object(messages[-1] if messages else "") or {}
        if task.agent_role == AgentRole.PLANNER.value:
            candidate = raw.get("proposal") or raw
            try:
                proposal = PlannerProposalContract.model_validate(candidate)
            except Exception:
                proposal = None
            if proposal:
                result = AgentTaskResultContract(task_id=task.id, status=AgentTaskStatus.COMPLETED, proposed_next_action={"proposal": proposal.model_dump(mode="json")}, handoff_summary="Codex Planner returned a validated proposal.")
            else:
                result = AgentTaskResultContract(task_id=task.id, status=AgentTaskStatus.NEED_REPLAN, proposed_next_action={"raw": raw}, handoff_summary="Codex Planner output was not a valid proposal.")
        elif task.task_kind in {AgentTaskKind.PLAN_REVIEW.value, AgentTaskKind.RESULT_REVIEW.value}:
            candidate = raw.get("review") or raw
            try:
                review = AnalysisReviewContract.model_validate(candidate)
                result = AgentTaskResultContract(task_id=task.id, status=AgentTaskStatus.COMPLETED, proposed_next_action={"review": review.model_dump(mode="json")}, handoff_summary="Codex Analysis returned a validated review.")
            except Exception:
                result = AgentTaskResultContract(task_id=task.id, status=AgentTaskStatus.NEED_REPLAN, proposed_next_action={"raw": raw}, handoff_summary="Codex Analysis output was not a valid review.")
        else:
            result = self._normalize_result(task.id, raw, messages[-1] if messages else "")
        return result, {"provider_request_id": thread_id, "thread_id": thread_id, "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"), "latency_ms": None, "message": messages[-1] if messages else "", "action": result.proposed_next_action}

    @staticmethod
    def _normalize_result(task_id: str, raw: dict[str, Any], message: str) -> AgentTaskResultContract:
        """Keep provider drift inside the adapter and preserve the audit trail."""
        try:
            return AgentTaskResultContract.model_validate({"task_id": task_id, **raw})
        except Exception:
            raw_status = str(raw.get("status") or "NEED_REPLAN").upper()
            try:
                status = AgentTaskStatus(raw_status)
            except ValueError:
                status = AgentTaskStatus.NEED_REPLAN

            def objects(value: Any) -> list[dict[str, Any]]:
                if not isinstance(value, list):
                    return []
                return [item if isinstance(item, dict) else {"summary": str(item)[:2000]} for item in value]

            failure = raw.get("failure_classification")
            if status in {AgentTaskStatus.FAILED, AgentTaskStatus.BLOCKED} and not isinstance(failure, dict):
                failure = {"fingerprint": "model-output-normalized", "classification": "MODEL_OUTPUT_SCHEMA_DRIFT", "retryable": True, "reason": "Provider returned a non-contract result shape.", "next_allowed_condition": "retry or replan with the same durable evidence"}
            if status == AgentTaskStatus.NEED_REPLAN:
                failure = None
            return AgentTaskResultContract(
                task_id=task_id, status=status, new_facts=objects(raw.get("new_facts")),
                updated_hypotheses=objects(raw.get("updated_hypotheses")),
                accepted_solution_steps=objects(raw.get("accepted_solution_steps")),
                rejected_paths=objects(raw.get("rejected_paths")), evidence_ids=[str(item) for item in raw.get("evidence_ids") or []],
                failure_classification=failure, proposed_next_action=raw.get("proposed_next_action") if isinstance(raw.get("proposed_next_action"), dict) else {"raw": raw},
                handoff_summary=str(raw.get("handoff_summary") or message or "Provider result was normalized by the role adapter.")[:4000],
            )

    async def _openai(self, session, run: SolveRun, challenge: Challenge, attempt: RunAttempt, task: AgentTask, memory: dict, lease_token: str, prompt: str) -> tuple[AgentTaskResultContract, dict]:
        if not isinstance(self.engine, OpenAICompatibleEngine):
            raise RuntimeError("OPENAI_RUNTIME_ENGINE_REQUIRED")
        messages = [{"role": "system", "content": prompt}]
        last_action: Any = None
        trace: dict[str, Any] = {}
        for _ in range(min(4, max(1, int((task.budget_json or {}).get("max_internal_requests", 1))))):
            turn = await self._new_turn(session, run, task, json.dumps(messages, ensure_ascii=False))
            started = time.perf_counter()
            action = await self.engine.next_action(messages)
            trace = dict(self.engine.last_trace or {})
            trace.setdefault("latency_ms", round((time.perf_counter() - started) * 1000))
            await self._finish_turn(session, run, turn, trace, action.model_dump(mode="json"))
            last_action = action
            if getattr(action, "type", "") != "tool":
                break
            if action.tool_name not in (task.allowed_tools_json or []):
                return AgentTaskResultContract(task_id=task.id, status=AgentTaskStatus.BLOCKED, failure_classification={"fingerprint": "role-tool-scope", "classification": "POLICY_BLOCK", "retryable": False, "reason": f"{action.tool_name} is outside the task contract", "next_allowed_condition": "planner must declare the tool"}, handoff_summary="Model selected a tool outside its task scope."), trace
            result = await self.tool_invoker(session, run, challenge, action.tool_name, action.arguments, execution_layer="multi_agent", logical_tool_call_id=f"mcp:{run.id}:{attempt.id}:agent-task:{task.id}:{uuid.uuid4().hex[:8]}", agent_task_id=task.id, agent_role=task.agent_role, task_lease_token=lease_token)
            messages.extend([{"role": "assistant", "content": json.dumps(action.model_dump(mode="json"), ensure_ascii=False)}, {"role": "user", "content": json.dumps({"tool_result": result}, ensure_ascii=False, default=str)}])
        if task.agent_role == AgentRole.PLANNER.value:
            proposal = _json_object(getattr(last_action, "summary", "") or "") or {}
            return AgentTaskResultContract(task_id=task.id, status=AgentTaskStatus.NEED_REPLAN, proposed_next_action={"raw": proposal}, handoff_summary="OpenAI Planner must return a proposal object in its final JSON."), trace
        return AgentTaskResultContract(task_id=task.id, status=AgentTaskStatus.COMPLETED, proposed_next_action={"action": last_action.model_dump(mode="json") if last_action else {}}, handoff_summary="OpenAI role turn completed."), trace

    async def execute(self, session, run: SolveRun, challenge: Challenge, attempt: RunAttempt, task: AgentTask, lease_token: str) -> AgentTaskResultContract:
        policy = await self._policy(session, task)
        memory = await deterministic_controller.memory.read_for_role(session, run.id, task.agent_role)
        prompt = self._prompt(task, policy, memory, challenge)
        if run.engine_type == "codex_sdk":
            turn = await self._new_turn(session, run, task, prompt)
            try:
                result, trace = await asyncio.wait_for(self._codex(session, run, challenge, task, memory, lease_token, prompt), timeout=max(10, int(task.timeout_seconds or 120)))
            except asyncio.TimeoutError:
                result = AgentTaskResultContract(task_id=task.id, status=AgentTaskStatus.BLOCKED, failure_classification={"fingerprint": "codex-role-timeout", "classification": "MODEL_TIMEOUT", "retryable": True, "reason": "Codex role turn exceeded its leased task timeout", "next_allowed_condition": "retry with a fresh role thread"}, handoff_summary="Codex role turn timed out; controller may replan.")
                trace = {"parse_error_code": "ROLE_MODEL_TIMEOUT", "response_excerpt": "", "latency_ms": int(task.timeout_seconds or 120) * 1000}
            await self._finish_turn(session, run, turn, trace, result.proposed_next_action)
            return result
        if run.engine_type == "openai_compatible":
            result, _ = await self._openai(session, run, challenge, attempt, task, memory, lease_token, prompt)
            return result
        turn = await self._new_turn(session, run, task, prompt)
        result, trace = await self._mock(session, run, challenge, attempt, task, memory, lease_token)
        await self._finish_turn(session, run, turn, trace, result.proposed_next_action)
        return result
