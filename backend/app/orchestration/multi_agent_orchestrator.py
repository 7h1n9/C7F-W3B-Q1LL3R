"""Durable, model-backed multi-agent controller.

The controller is intentionally policy-oriented: it creates and leases tasks,
persists model contracts, records evidence, and applies the finish gate.  It
does not invent a fixed GET sequence and it never treats a non-empty evidence
list as an automatic Analysis approval.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select

from app.core.database import SessionLocal
from app.core.exceptions import DomainError
from app.models.challenge import Challenge
from app.models.multi_agent import (
    AgentTask,
    AgentTaskResult,
    AnalysisReview,
    ApprovedAction,
    EvidenceLedger,
    PlannerProposal,
    SolutionChainNode,
    VerifiedFact,
)
from app.models.run import (
    Artifact,
    FlagCandidate,
    Hypothesis,
    Observation,
    RunAttempt,
    RunEvent,
    RunExecutionLease,
    SolveRun,
    ToolCall,
)
from app.orchestration.role_agent_runtime import RoleAgentRuntime
from app.orchestration.state_machine import RunStatus, transition
from app.schemas.multi_agent import (
    AgentRole,
    AgentTaskContract,
    AgentTaskKind,
    AgentTaskResultContract,
    AgentTaskStatus,
    AnalysisDecision,
    AnalysisReviewContract,
    EvidenceLedgerContract,
    PlannerProposalContract,
    ProductionResultContext,
    TaskBudget,
)
from app.services.action_fingerprint import fingerprint_compiled_action
from app.services.approved_action_compiler import approved_action_compiler
from app.services.events import event_service
from app.services.failure_classification import normalize_failure_classification
from app.services.multi_agent import deterministic_controller
from app.services.solver_state import solver_state_service
from app.services.run_finalizer import run_finalizer
from app.services.tool_result_fact_reducer import tool_result_fact_reducer
from app.services.tool_failure_policy import record_tool_failure
from app.tools.gateway import tool_gateway

logger = logging.getLogger(__name__)


class MultiAgentOrchestrator:
    def __init__(self, tool_invoker=None, runtime: RoleAgentRuntime | None = None) -> None:
        self.tool_invoker = tool_invoker or tool_gateway.invoke
        self.runtime = runtime or RoleAgentRuntime(tool_invoker=self.tool_invoker)

    async def _status(self, session, run: SolveRun, target: RunStatus) -> None:
        if RunStatus(run.status) == target:
            return
        transition(run, target)
        await session.commit()
        await self._controller_event(session, run.id, "run.status_changed", {"status": run.status, "controller": "multi_agent_v1"})

    async def _phase(self, session, run: SolveRun, phase: str) -> None:
        previous = run.current_phase
        run.current_phase = phase
        checkpoint = dict(run.recovery_checkpoint_json or {})
        checkpoint["current_phase"] = phase
        if phase == "MYSQL_METADATA_DISCOVERY":
            checkpoint["checkpoint_type"] = "MYSQL_METADATA_DISCOVERY_ACTIVE"
        elif phase == "BOUNDED_EXTRACTION":
            checkpoint["checkpoint_type"] = "BOUNDED_EXTRACTION_ACTIVE"
        elif phase == "FLAG_SEARCH":
            checkpoint["checkpoint_type"] = "FLAG_SEARCH_ACTIVE"
        run.recovery_checkpoint_json = checkpoint
        state = await solver_state_service.load(session, run.id)
        if state is not None:
            state.current_phase = phase
            plan = dict(state.run_plan_json or {})
            plan["current_phase"] = phase
            state.run_plan_json = plan
        await session.commit()
        if str(previous or "") != phase:
            await self._controller_event(session, run.id, "run.phase_changed", {"previous_phase": previous, "phase": phase, "source": "role_agent_runtime"})

    async def _controller_event(self, session, run_id: str, event_type: str, payload: dict) -> None:
        """Persist controller milestones without blocking on SSE fanout.

        The ordinary EventService also publishes to in-process subscribers.
        Result Context construction is a correctness boundary, so its audit
        rows are committed directly in the current MySQL transaction and are
        never allowed to wait on an event-stream consumer.
        """
        body = payload or {}
        encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, default=str).encode()
        sequence = int(await session.scalar(select(func.max(RunEvent.sequence)).where(RunEvent.run_id == run_id)) or 0) + 1
        session.add(RunEvent(
            run_id=run_id,
            sequence=sequence,
            event_type=event_type,
            payload_json=body,
            payload_size=len(encoded),
            payload_digest=hashlib.sha256(encoded).hexdigest(),
        ))
        await session.commit()

    async def _capability_phase(self, session, run: SolveRun) -> str:
        """Derive the next phase from durable evidence/capabilities, not role completion."""
        challenge = await session.get(Challenge, run.challenge_id)
        if self._asset_warranty_mysql(challenge):
            keys = set((await session.scalars(select(VerifiedFact.fact_key).where(
                VerifiedFact.run_id == run.id,
                VerifiedFact.promotion_status == "VERIFIED",
            ))).all())
            if "asset_warranty.valid_baseline" not in keys or "asset_warranty.invalid_baseline" not in keys:
                return "BUSINESS_BASELINE"
            if "asset_warranty.mysql_boolean_oracle" not in keys:
                return "BOOLEAN_ORACLE"
            if "asset_warranty.oracle_calibration_matrix" not in keys or "asset_warranty.mysql_dbms" not in keys:
                return "ORACLE_CALIBRATION"
            metadata_required = {
                "asset_warranty.mysql_version",
                "asset_warranty.mysql_version_comment",
                "asset_warranty.current_database",
                "asset_warranty.mysql_user_tables",
                "asset_warranty.mysql_candidate_columns",
            }
            if not metadata_required <= keys:
                return "MYSQL_METADATA_DISCOVERY"
            if await self._candidate_gate(session, run):
                return "FLAG_VERIFICATION"
            return "BOUNDED_EXTRACTION"
        state = await solver_state_service.load(session, run.id)
        ledger = state.capability_ledger_json if state else {}
        candidate = await self._candidate_gate(session, run)
        if candidate:
            return "FLAG_VERIFICATION"
        keys = {str(key).lower() for key in ledger}
        if "flag_search" in keys:
            return "FLAG_SEARCH"
        if any("boolean" in key or "oracle" in key for key in keys):
            return "CHAINING"
        evidence_count = int(await session.scalar(select(func.count(EvidenceLedger.id)).where(EvidenceLedger.run_id == run.id)) or 0)
        if evidence_count:
            hypothesis_count = int(await session.scalar(select(func.count()).select_from(Hypothesis).where(Hypothesis.run_id == run.id, Hypothesis.status.in_(["OPEN", "ACTIVE"]))) or 0)
            return "HYPOTHESIS" if hypothesis_count else "MAPPING"
        return "BASELINE"

    @staticmethod
    def _asset_warranty_mysql(challenge: Challenge | None) -> bool:
        metadata = (challenge.metadata_json or {}) if challenge else {}
        return (
            str(metadata.get("adapter") or "").lower() == "asset_warranty"
            and str(metadata.get("dbms") or "").lower() == "mysql"
        )

    @staticmethod
    def _looks_like_asset_warranty_challenge(challenge: Challenge | None) -> bool:
        if challenge is None:
            return False
        text = f"{challenge.name or ''}\n{challenge.description or ''}".lower()
        return "资产保修" in text or "asset warranty" in text or "asset_no" in text or "department" in text

    async def _ensure_asset_warranty_metadata_or_pause(self, session, run: SolveRun, challenge: Challenge | None) -> bool:
        if not self._looks_like_asset_warranty_challenge(challenge) or self._asset_warranty_mysql(challenge):
            return True
        required = {
            "adapter": "asset_warranty",
            "dbms": "mysql",
            "endpoint": "/api/warranty/check",
            "method": "POST",
            "content_type": "application/json",
            "fields": ["asset_no", "department"],
            "control_values": {"asset_no": "PC-2026-013", "department": "OPS"},
        }
        run.last_error_code = "CHALLENGE_METADATA_REQUIRED"
        run.last_error_message = "Asset warranty challenge requires normalized metadata_json before multi_agent_v1 can route the specialized chain."
        run.recovery_checkpoint_json = {"classification": "CHALLENGE_METADATA_REQUIRED", "required_metadata": required}
        await self._status(session, run, RunStatus.PAUSED_CHECKPOINT)
        await session.commit()
        return False

    async def _asset_warranty_mysql_finish_ready(self, session, run: SolveRun, challenge: Challenge | None) -> bool:
        if not self._asset_warranty_mysql(challenge):
            return True
        state = await solver_state_service.load(session, run.id)
        ledger = state.capability_ledger_json if state else {}
        keys = set((await session.scalars(select(VerifiedFact.fact_key).where(
            VerifiedFact.run_id == run.id,
            VerifiedFact.promotion_status == "VERIFIED",
        ))).all())
        required_facts = {
            "asset_warranty.valid_baseline",
            "asset_warranty.invalid_baseline",
            "asset_warranty.mysql_boolean_oracle",
            "asset_warranty.mysql_dbms",
            "asset_warranty.mysql_version",
            "asset_warranty.mysql_version_comment",
            "asset_warranty.current_database",
            "asset_warranty.mysql_user_tables",
            "asset_warranty.mysql_candidate_columns",
        }
        return required_facts <= keys and "mysql_metadata_discovered" in ledger

    def _max_replan_cycles(self, run: SolveRun, challenge: Challenge | None) -> int:
        configured = max(3, int(run.max_agent_steps or 8) // 2)
        if self._asset_warranty_mysql(challenge):
            return max(24, configured)
        return max(3, min(8, configured))

    async def _handle_mysql_metadata_empty_result(self, session, run: SolveRun, challenge: Challenge, approved: ApprovedAction, task: AgentTask) -> bool:
        """Bound consecutive empty stage results and create a resumable pause."""
        if not self._asset_warranty_mysql(challenge) or approved.tool_name != "mysql_metadata_discovery":
            return False
        args = approved.compiled_arguments_json or {}
        stage = str(args.get("stage") or "").lower()
        if stage not in {"version", "version_comment", "database", "tables", "columns"}:
            return False
        checkpoint = dict(run.recovery_checkpoint_json or {})
        attempts = dict(checkpoint.get("metadata_attempts") or {})
        previous_stage = str(checkpoint.get("metadata_last_empty_stage") or "")
        state = await solver_state_service.load(session, run.id)
        ledger = dict(state.capability_ledger_json or {}) if state else {}
        empty_ledger = dict(ledger.get("mysql_metadata_empty_results") or {})
        prior = dict(empty_ledger.get(stage) or {})
        attempts[stage] = int(attempts.get(stage) or 0) + 1 if previous_stage == stage else 1
        call = await session.scalar(select(ToolCall).where(ToolCall.run_id == run.id, ToolCall.agent_task_id == task.id).order_by(ToolCall.created_at.desc()))
        empty_ledger[stage] = {
            "count": attempts[stage],
            "tool_call_ids": [*(prior.get("tool_call_ids") or []), *([call.id] if call else [])][-10:],
            "target_expression": args.get("target_expression"),
        }
        if state is not None:
            state.capability_ledger_json = {**ledger, "mysql_metadata_empty_results": empty_ledger}
        checkpoint.update({"metadata_attempts": attempts, "metadata_last_empty_stage": stage})
        if attempts[stage] >= 2:
            target = {
                "version": "VERSION()", "version_comment": "@@version_comment",
                "database": "DATABASE()", "tables": "information_schema.tables",
                "columns": "information_schema.columns",
            }[stage]
            run.last_error_code = "MYSQL_METADATA_STAGE_EMPTY_RESULT"
            run.last_error_message = "Tool completed but produced no metadata facts."
            run.recovery_checkpoint_json = {
                "checkpoint_type": "MYSQL_METADATA_STAGE_EMPTY_RESULT",
                "current_phase": "WAITING_USER",
                "stage": stage,
                "target_expression": target,
                "attempts": attempts[stage],
                "reason": "Tool completed but produced no metadata facts",
                "next_allowed_condition": "Fix mysql_metadata_discovery result contract or runner extraction",
                "task_id": task.id,
                "question": "metadata extractor returned empty result twice",
                "options": ["retry_after_fix", "finish_unsolved_wp", "try_alternative_strategy"],
            }
            await self._phase(session, run, "WAITING_USER")
            await self._status(session, run, RunStatus.WAITING_USER)
            await session.commit()
            return True
        run.recovery_checkpoint_json = checkpoint
        await self._phase(session, run, "MYSQL_METADATA_DISCOVERY")
        if RunStatus(run.status) == RunStatus.EXECUTING:
            await self._status(session, run, RunStatus.EVALUATING)
        await self._status(session, run, RunStatus.PLANNING)
        await session.commit()
        return False

    async def _task(self, session, run: SolveRun, role: AgentRole, kind: AgentTaskKind, objective: str, tools: list[str], *, parent: str | None = None, context: dict | None = None, budget: TaskBudget | None = None, success_condition: str = "produce a validated structured handoff with evidence or an explicit failure classification", stop_conditions: list[str] | None = None) -> tuple[AgentTask, str]:
        logger.warning("multi_agent.task.snapshot.begin run_id=%s role=%s", run.id, role.value)
        snapshot = await deterministic_controller.memory.read_snapshot(session, run.id)
        logger.warning("multi_agent.task.snapshot.done run_id=%s snapshot_id=%s", run.id, snapshot.id if snapshot else None)
        logger.warning("multi_agent.task.policy.begin run_id=%s role=%s", run.id, role.value)
        policy = await deterministic_controller.permissions.policy(session, role)
        logger.warning("multi_agent.task.policy.done run_id=%s role=%s", run.id, role.value)
        selected_budget = budget or TaskBudget(max_logical_calls=0, max_internal_requests=min(8, policy.max_internal_requests), max_runtime_seconds=min(300, policy.max_runtime_seconds))
        max_calls = min(int(selected_budget.max_logical_calls), int(policy.max_logical_calls)) if tools else 0
        contract = AgentTaskContract(
            task_id=f"AT-{role.value}-{uuid.uuid4().hex[:12]}", run_id=run.id, agent_role=role, task_kind=kind,
            objective=objective, allowed_tools=tools, created_by_task_id=parent,
            evidence_snapshot_id=snapshot.id if snapshot else None,
            input_snapshot_version=snapshot.version if snapshot else 0,
            budget=selected_budget.model_copy(update={"max_logical_calls": max_calls}),
            success_condition=success_condition,
            stop_conditions=stop_conditions or ["honor task budget", "stop after one discriminating experiment"], context=context or {},
        )
        logger.warning("multi_agent.task.create.begin run_id=%s task_id=%s role=%s kind=%s parent=%s", run.id, contract.task_id, role.value, kind.value, parent)
        item = await deterministic_controller.create_task(session, contract)
        logger.warning("multi_agent.task.create.flushed run_id=%s task_id=%s", run.id, item.id)
        token = await deterministic_controller.claim_task(session, item.id, "role-agent-runtime", lease_seconds=contract.budget.max_runtime_seconds)
        if role == AgentRole.EXPLOIT and "sql_boolean_compare" in tools:
            # Boolean comparison is a bounded multi-request operation.  Its
            # production lease must cover the dispatch itself, not the short
            # generic 45-second idle window used by model turns.
            item.idle_deadline_at = datetime.now(UTC) + timedelta(seconds=min(300, int(contract.budget.max_runtime_seconds)))
            await session.commit()
        logger.warning("multi_agent.task.claimed run_id=%s task_id=%s", run.id, item.id)
        return item, token

    async def _evidence_for_task(self, session, run: SolveRun, task: AgentTask) -> tuple[list[str], list[tuple[ToolCall, Artifact]]]:
        pairs: list[tuple[ToolCall, Artifact]] = []
        calls = list((await session.scalars(select(ToolCall).where(ToolCall.run_id == run.id, ToolCall.agent_task_id == task.id).order_by(ToolCall.created_at))).all())
        for call in calls:
            artifact = await session.scalar(select(Artifact).where(Artifact.run_id == run.id, Artifact.tool_call_id == call.id).order_by(Artifact.created_at.desc()))
            if artifact:
                pairs.append((call, artifact))
        evidence_ids: list[str] = []
        for call, artifact in pairs:
            existing = await session.scalar(select(EvidenceLedger).where(EvidenceLedger.run_id == run.id, EvidenceLedger.tool_call_id == call.id, EvidenceLedger.agent_task_id == task.id))
            if existing:
                evidence_ids.append(existing.id)
                continue
            semantic = {
                "http_request": "HTTP request response",
                "content_discovery": "Public content discovery response",
                "sql_boolean_compare": "SQL TRUE/FALSE oracle comparison response",
                "oracle_probe_matrix": "Bounded oracle probe matrix response",
            }.get(call.tool_name, f"{call.tool_name} tool response")
            item = await deterministic_controller.evidence.record(session, EvidenceLedgerContract(
                evidence_id=f"E-{uuid.uuid4().hex[:12]}", run_id=run.id, evidence_type="TOOL_ARTIFACT", artifact_id=artifact.id,
                tool_call_id=call.id, agent_task_id=task.id, summary=(artifact.summary or semantic)[:4000], sha256=artifact.sha256, source_chain=[artifact.id, call.id, task.id],
            ))
            evidence_ids.append(item.id)
        return evidence_ids, pairs

    async def _complete(self, session, run: SolveRun, task: AgentTask, token: str, result: AgentTaskResultContract) -> AgentTaskResultContract:
        evidence_ids, pairs = await self._evidence_for_task(session, run, task)
        if evidence_ids:
            result = result.model_copy(update={"evidence_ids": sorted(set((result.evidence_ids or []) + evidence_ids))})
        candidates = await tool_result_fact_reducer.reduce(session, run, await session.get(Challenge, run.challenge_id), task, result.evidence_ids)
        if candidates:
            result = result.model_copy(update={"new_facts": [*(result.new_facts or []), *candidates]})
        await deterministic_controller.complete_task(session, task.id, result, token)
        await event_service.append(session, run.id, "agent.task.completed", {"task_id": task.id, "agent_role": task.agent_role, "task_kind": task.task_kind, "status": result.status.value, "evidence_ids": result.evidence_ids})
        return result

    @staticmethod
    def _mysql_boolean_stage(challenge: Challenge, *, current_stage: str, next_agent: str) -> bool:
        metadata = challenge.metadata_json or {}
        return (
            str(metadata.get("adapter") or "").lower() == "asset_warranty"
            and str(metadata.get("dbms") or "").lower() == "mysql"
            and str(current_stage or "").upper() == "BOOLEAN_ORACLE"
        )

    @staticmethod
    def _mysql_metadata_stage(challenge: Challenge, *, current_stage: str, next_agent: str) -> bool:
        metadata = challenge.metadata_json or {}
        return (
            str(metadata.get("adapter") or "").lower() == "asset_warranty"
            and str(metadata.get("dbms") or "").lower() == "mysql"
            and str(current_stage or "").upper() == "MYSQL_METADATA_DISCOVERY"
            and str(next_agent or "").upper() == AgentRole.EXPLOIT.value
        )

    @staticmethod
    def _oracle_calibration_stage(challenge: Challenge, *, current_stage: str, next_agent: str) -> bool:
        metadata = challenge.metadata_json or {}
        return (
            str(metadata.get("adapter") or "").lower() == "asset_warranty"
            and str(metadata.get("dbms") or "").lower() == "mysql"
            and str(current_stage or "").upper() == "ORACLE_CALIBRATION"
            and str(next_agent or "").upper() == AgentRole.EXPLOIT.value
        )

    async def _oracle_calibration_plan(self, session, run: SolveRun) -> dict[str, object]:
        oracle_fact = await session.scalar(select(VerifiedFact).where(
            VerifiedFact.run_id == run.id,
            VerifiedFact.fact_key == "asset_warranty.mysql_boolean_oracle",
            VerifiedFact.promotion_status == "VERIFIED",
        ))
        if oracle_fact is None:
            raise DomainError("BOOLEAN_ORACLE_REQUIRED", "Expression calibration requires the verified Web Boolean Oracle.")
        evidence_ids = list(oracle_fact.evidence_ids_json or [])
        hypothesis = await session.scalar(select(Hypothesis).where(
            Hypothesis.run_id == run.id,
            Hypothesis.category == "ORACLE_CALIBRATION",
        ).order_by(Hypothesis.created_at.desc()))
        if hypothesis is None:
            hypothesis = Hypothesis(
                run_id=run.id,
                category="ORACLE_CALIBRATION",
                title="The verified predicate template supports progressively richer SQL expressions.",
                description="Calibrate literal, arithmetic, scalar-function, scalar-subquery, MySQL fingerprint and information_schema predicates before metadata discovery.",
                confidence=90,
                priority=100,
                status="OPEN",
                evidence_json={"fact_ids": [oracle_fact.id], "evidence_ids": evidence_ids, "expected_dbms": "mysql"},
            )
            session.add(hypothesis)
            await session.flush()
        return {"oracle_fact_id": oracle_fact.id, "evidence_ids": evidence_ids, "source_hypothesis_id": hypothesis.id}

    async def _mysql_metadata_plan(self, session, run: SolveRun) -> dict[str, object]:
        """Return the next metadata expression from durable verified facts."""
        oracle_fact = await session.scalar(select(VerifiedFact).where(
            VerifiedFact.run_id == run.id,
            VerifiedFact.fact_key == "asset_warranty.mysql_boolean_oracle",
            VerifiedFact.promotion_status == "VERIFIED",
        ))
        if oracle_fact is None:
            raise DomainError("BOOLEAN_ORACLE_REQUIRED", "MySQL metadata discovery requires the verified asset-warranty Boolean Oracle.")
        evidence_ids = list(oracle_fact.evidence_ids_json or [])
        hypothesis = await session.scalar(select(Hypothesis).where(
            Hypothesis.run_id == run.id,
            Hypothesis.category == "MYSQL_METADATA",
        ).order_by(Hypothesis.created_at.desc()))
        if hypothesis is None:
            hypothesis = Hypothesis(
                run_id=run.id,
                category="MYSQL_METADATA",
                title="The confirmed Web Boolean Oracle can read current MySQL metadata.",
                description="Use only VERSION(), @@version_comment, DATABASE(), and current-database information_schema scopes.",
                confidence=90,
                priority=100,
                status="OPEN",
                evidence_json={"fact_ids": [oracle_fact.id], "evidence_ids": evidence_ids, "dbms": "mysql"},
            )
            session.add(hypothesis)
            await session.flush()
        verified = set((await session.scalars(select(VerifiedFact.fact_key).where(
            VerifiedFact.run_id == run.id,
            VerifiedFact.promotion_status == "VERIFIED",
        ))).all())
        if "asset_warranty.mysql_version" not in verified:
            target, stage = "VERSION()", "version"
        elif "asset_warranty.mysql_version_comment" not in verified:
            target, stage = "@@version_comment", "version_comment"
        elif "asset_warranty.current_database" not in verified:
            target, stage = "DATABASE()", "database"
        elif "asset_warranty.mysql_user_tables" not in verified:
            target, stage = "information_schema.tables", "tables"
        elif "asset_warranty.mysql_candidate_columns" not in verified:
            table_fact = await session.scalar(select(VerifiedFact).where(
                VerifiedFact.run_id == run.id,
                VerifiedFact.fact_key == "asset_warranty.mysql_user_tables",
                VerifiedFact.promotion_status == "VERIFIED",
            ))
            tables = ((table_fact.value_json or {}).get("tables") if table_fact and isinstance(table_fact.value_json, dict) else []) or []
            candidate_table = next((str(item.get("name")) for item in tables if isinstance(item, dict) and item.get("name")), "")
            if not candidate_table:
                raise DomainError("MYSQL_USER_TABLES_REQUIRED", "Column discovery requires at least one verified current-database user table.")
            target, stage = "information_schema.columns", "columns"
        else:
            target, stage = "information_schema.columns", "columns"
        return {
            "oracle_fact_id": oracle_fact.id,
            "evidence_ids": evidence_ids,
            "source_hypothesis_id": hypothesis.id,
            "target_expression": target,
            "stage": stage,
        }

    async def _proposal(self, session, run: SolveRun, challenge: Challenge, task: AgentTask, result: AgentTaskResultContract) -> PlannerProposal:
        raw = (result.proposed_next_action or {}).get("proposal") or {}
        try:
            contract = PlannerProposalContract.model_validate(raw)
        except Exception as error:
            raise DomainError("MODEL_OUTPUT_SCHEMA_INVALID", f"PlannerProposalContract is invalid: {error}", {"task_id": task.id}) from error
        metadata = challenge.metadata_json or {}
        if (
            self._asset_warranty_mysql(challenge)
        ):
            state = await solver_state_service.load(session, run.id)
            ledger = state.capability_ledger_json if state else {}
            verified_keys = set((await session.scalars(select(VerifiedFact.fact_key).where(
                VerifiedFact.run_id == run.id,
                VerifiedFact.promotion_status == "VERIFIED",
                VerifiedFact.fact_key.in_(("asset_warranty.valid_baseline", "asset_warranty.invalid_baseline")),
            ))).all())
            oracle_fact = await session.scalar(select(VerifiedFact).where(
                VerifiedFact.run_id == run.id,
                VerifiedFact.fact_key == "asset_warranty.mysql_boolean_oracle",
                VerifiedFact.promotion_status == "VERIFIED",
            ))
            # The model may suggest a later stage from stale working memory.
            # The Controller owns the business-baseline ordering and must not
            # dispatch Boolean Oracle until both response baselines are
            # durable VERIFIED facts.
            if "asset_warranty.valid_baseline" not in verified_keys:
                contract = contract.model_copy(update={
                    "current_stage": "BUSINESS_BASELINE",
                    "next_agent": AgentRole.RECON,
                    "objective": "Execute the valid asset-warranty business baseline from challenge metadata.",
                    "allowed_tools": ["http_request"],
                    "required_capabilities": [],
                    "success_condition": "Confirm the valid business baseline with durable HTTP evidence.",
                })
            elif "asset_warranty.invalid_baseline" not in verified_keys:
                contract = contract.model_copy(update={
                    "current_stage": "BUSINESS_BASELINE",
                    "next_agent": AgentRole.RECON,
                    "objective": "Execute one invalid asset-warranty baseline by changing exactly one declared business field.",
                    "allowed_tools": ["http_request"],
                    "required_capabilities": ["valid_business_baseline_confirmed"],
                    "success_condition": "Confirm the invalid business baseline and response differential with durable HTTP evidence.",
                })
            elif (
                oracle_fact is None
                and "mysql_boolean_oracle_confirmed" not in ledger
            ):
                contract = contract.model_copy(update={
                    "current_stage": "BOOLEAN_ORACLE",
                    "next_agent": AgentRole.EXPLOIT,
                    "objective": "Use the confirmed asset-warranty business response differential to test one declared field as a stable MySQL Boolean Oracle.",
                    "allowed_tools": ["sql_boolean_compare"],
                    "required_capabilities": ["business_response_differential_confirmed"],
                    "success_condition": "Confirm a stable paired TRUE/FALSE Boolean Oracle from the asset-warranty POST contract.",
                    "budget": contract.budget.model_copy(update={"max_logical_calls": 1, "max_internal_requests": 12, "max_runtime_seconds": 300}),
                })
            elif "mysql_boolean_oracle_confirmed" in ledger and "mysql_dbms_confirmed" not in ledger:
                calibration_plan = await self._oracle_calibration_plan(session, run)
                contract = contract.model_copy(update={
                    "current_stage": "ORACLE_CALIBRATION",
                    "next_agent": AgentRole.EXPLOIT,
                    "objective": "Calibrate the verified Boolean predicate through Level 0 to Level 5 before any MySQL metadata query.",
                    "allowed_tools": ["oracle_expression_calibration"],
                    "required_capabilities": ["boolean_predicate_oracle_confirmed"],
                    "input_fact_ids": [str(calibration_plan["oracle_fact_id"])],
                    "input_evidence_ids": list(calibration_plan["evidence_ids"]),
                    "success_condition": "All calibration levels pass with stable paired TRUE/FALSE response signatures.",
                    "budget": contract.budget.model_copy(update={"max_logical_calls": 1, "max_internal_requests": 40, "max_runtime_seconds": 600}),
                })
            elif "mysql_boolean_oracle_confirmed" in ledger and "mysql_metadata_discovered" not in ledger:
                metadata_plan = await self._mysql_metadata_plan(session, run)
                contract = contract.model_copy(update={
                    "current_stage": "MYSQL_METADATA_DISCOVERY",
                    "next_agent": AgentRole.EXPLOIT,
                    "objective": f"Discover MySQL metadata using the verified Web Boolean Oracle for {metadata_plan['target_expression']}.",
                    "allowed_tools": ["mysql_metadata_discovery"],
                    "required_capabilities": ["mysql_dbms_confirmed", "scalar_subquery_oracle_confirmed"],
                    "input_fact_ids": [str(metadata_plan["oracle_fact_id"])],
                    "input_evidence_ids": list(metadata_plan["evidence_ids"]),
                    "success_condition": f"Complete bounded MySQL metadata discovery for {metadata_plan['target_expression']} within the current-database scope.",
                    "budget": contract.budget.model_copy(update={"max_logical_calls": 1, "max_internal_requests": 8, "max_runtime_seconds": 300}),
                })
            else:
                # Metadata discovery is a prerequisite, not a finish gate. Once
                # it is complete, let the Planner continue into extraction or
                # verification instead of forcing the Boolean Oracle stage.
                pass
        # The MySQL Boolean Oracle is a typed controller stage.  Do not let a
        # model fall back to http_request after the business differential has
        # already been established; the compiler and gateway must receive the
        # sql_boolean_compare contract for this stage.
        if self._mysql_boolean_stage(challenge, current_stage=contract.current_stage, next_agent=contract.next_agent.value):
            required = list(contract.required_capabilities or [])
            if "business_response_differential_confirmed" not in required:
                required.append("business_response_differential_confirmed")
            budget = contract.budget.model_copy(
                update={
                    "max_logical_calls": max(1, int(contract.budget.max_logical_calls or 0)),
                    "max_internal_requests": max(8, int(contract.budget.max_internal_requests or 0)),
                    "max_runtime_seconds": max(300, int(contract.budget.max_runtime_seconds or 0)),
                }
            )
            contract = contract.model_copy(update={"next_agent": AgentRole.EXPLOIT, "allowed_tools": ["sql_boolean_compare"], "required_capabilities": required, "budget": budget})
        if self._mysql_metadata_stage(challenge, current_stage=contract.current_stage, next_agent=contract.next_agent.value):
            contract = contract.model_copy(update={
                "next_agent": AgentRole.EXPLOIT,
                "allowed_tools": ["mysql_metadata_discovery"],
                "required_capabilities": ["mysql_dbms_confirmed", "scalar_subquery_oracle_confirmed"],
                "budget": contract.budget.model_copy(update={"max_logical_calls": 1, "max_internal_requests": 8, "max_runtime_seconds": 300}),
            })
        if self._oracle_calibration_stage(challenge, current_stage=contract.current_stage, next_agent=contract.next_agent.value):
            contract = contract.model_copy(update={
                "next_agent": AgentRole.EXPLOIT,
                "allowed_tools": ["oracle_expression_calibration"],
                "required_capabilities": ["boolean_predicate_oracle_confirmed"],
                "budget": contract.budget.model_copy(update={"max_logical_calls": 1, "max_internal_requests": 40, "max_runtime_seconds": 600}),
            })
        # The model-visible proposal_id may repeat on a later Run.  The
        # relational key must stay globally unique because Review and
        # ApprovedAction reference the PlannerProposal row.
        facts = set((await session.scalars(select(VerifiedFact.id).where(VerifiedFact.run_id == run.id, VerifiedFact.id.in_(contract.input_fact_ids)))).all()) if contract.input_fact_ids else set()
        evidence = set((await session.scalars(select(EvidenceLedger.id).where(EvidenceLedger.run_id == run.id, EvidenceLedger.id.in_(contract.input_evidence_ids)))).all()) if contract.input_evidence_ids else set()
        if len(facts) != len(set(contract.input_fact_ids)) or len(evidence) != len(set(contract.input_evidence_ids)):
            raise DomainError("PLANNER_REFERENCE_TYPE_INVALID", "Planner references must use VerifiedFact IDs and EvidenceLedger IDs in separate fields.", {"input_fact_ids": contract.input_fact_ids, "input_evidence_ids": contract.input_evidence_ids})
        row = PlannerProposal(id=str(uuid.uuid4()), run_id=run.id, proposal_id=contract.proposal_id, current_stage=contract.current_stage, decision_question=contract.decision_question, next_agent=contract.next_agent.value, objective=contract.objective, input_fact_ids_json=contract.input_fact_ids, input_evidence_ids_json=contract.input_evidence_ids, required_capabilities_json=contract.required_capabilities, allowed_tools_json=contract.allowed_tools, budget_json=contract.budget.model_dump(), success_condition=contract.success_condition, stop_conditions_json=contract.stop_conditions, fallback=contract.fallback, created_by_task_id=task.id)
        session.add(row)
        await session.flush()
        return row

    async def _review(self, session, run: SolveRun, proposal: PlannerProposal, task: AgentTask, result: AgentTaskResultContract) -> AnalysisReview:
        raw = (result.proposed_next_action or {}).get("review") or {}
        try:
            contract = AnalysisReviewContract.model_validate(raw)
        except Exception as error:
            raise DomainError("MODEL_OUTPUT_SCHEMA_INVALID", f"AnalysisReviewContract is invalid: {error}", {"task_id": task.id}) from error
        if task.task_kind == AgentTaskKind.RESULT_REVIEW.value:
            candidate_facts = (task.context_json or {}).get("candidate_facts") or []
            approved_indexes = list(contract.approved_fact_indexes or [])
            invalid_indexes = [index for index in approved_indexes if index < 0 or index >= len(candidate_facts)]
            if invalid_indexes:
                contract = contract.model_copy(update={
                    "decision": AnalysisDecision.REVISE,
                    "confidence": max(95, int(contract.confidence or 0)),
                    "approved_fact_indexes": [],
                    "reason": "Result Review approved fact indexes that do not reference the producing task candidate facts.",
                    "audit_reason": "RESULT_REVIEW_APPROVED_INDEX_OUT_OF_RANGE",
                    "next_phase": "HYPOTHESIS",
                })
            if (
                self._asset_warranty_mysql(await session.get(Challenge, run.challenge_id))
                and contract.decision == AnalysisDecision.APPROVE.value
                and not candidate_facts
                and not (contract.capabilities_added or [])
                and not contract.solution_step_accepted
            ):
                contract = contract.model_copy(update={
                    "decision": AnalysisDecision.REVISE,
                    "approved_fact_indexes": [],
                    "reason": "No candidate facts or capabilities were produced by this asset-warranty result.",
                    "audit_reason": "ASSET_WARRANTY_EMPTY_RESULT_REVIEW_BLOCKED",
                    "next_phase": "CHAINING",
                })
        if (
            task.task_kind == AgentTaskKind.PLAN_REVIEW.value
            and proposal.allowed_tools_json == ["http_compare"]
        ):
            arguments = contract.approved_arguments or {}
            if not isinstance(arguments.get("baseline"), dict) or not isinstance(arguments.get("candidate"), dict):
                contract = contract.model_copy(update={
                    "decision": AnalysisDecision.REVISE,
                    "confidence": max(95, int(contract.confidence or 0)),
                    "recommended_tool": None,
                    "approved_arguments": {},
                    "reason": "http_compare requires concrete baseline and candidate request/response objects; empty or abstract arguments cannot be compiled.",
                    "audit_reason": "HTTP_COMPARE_SCHEMA_PRECHECK_FAILED",
                    "next_phase": "HYPOTHESIS",
                })
        if (
            self._asset_warranty_mysql(await session.get(Challenge, run.challenge_id))
            and task.task_kind == AgentTaskKind.PLAN_REVIEW.value
            and str(proposal.current_stage or "").upper() in {"MAPPING", "HYPOTHESIS"}
            and proposal.next_agent == AgentRole.RECON.value
            and proposal.allowed_tools_json == ["http_request"]
        ):
            state = await solver_state_service.load(session, run.id)
            ledger = state.capability_ledger_json if state else {}
            if "business_response_differential_confirmed" in ledger and "mysql_boolean_oracle_confirmed" not in ledger:
                contract = contract.model_copy(update={
                    "decision": AnalysisDecision.REVISE,
                    "confidence": max(95, int(contract.confidence or 0)),
                    "recommended_tool": None,
                    "approved_arguments": {},
                    "reason": "Asset-warranty business baselines are already verified. Further HTTP mapping probes do not advance the solution chain; the next required stage is BOOLEAN_ORACLE with sql_boolean_compare.",
                    "audit_reason": "ASSET_WARRANTY_RECON_AFTER_BASELINE_BLOCKED",
                    "next_phase": "CHAINING",
                })
        if (
            str(proposal.current_stage or "").upper() == "MYSQL_METADATA_DISCOVERY"
            and proposal.allowed_tools_json == ["mysql_metadata_discovery"]
            and task.task_kind == AgentTaskKind.PLAN_REVIEW.value
        ):
            metadata = (await session.get(Challenge, run.challenge_id)).metadata_json or {}
            if str(metadata.get("adapter") or "").lower() == "asset_warranty" and str(metadata.get("dbms") or "").lower() == "mysql":
                plan = await self._mysql_metadata_plan(session, run)
                oracle_fact = await session.get(VerifiedFact, str(plan["oracle_fact_id"]))
                oracle_value = oracle_fact.value_json if oracle_fact and isinstance(oracle_fact.value_json, dict) else {}
                approved_arguments = {
                    "dbms": "mysql",
                    "discovery_scope": "current_database",
                    "target_expression": plan["target_expression"],
                    "expression_type": "METADATA_DISCOVERY",
                    "source_hypothesis_id": plan["source_hypothesis_id"],
                    "assumption_status": "VERIFIED",
                    "stage": plan["stage"],
                    "max_tables": 10,
                    "max_columns": 30,
                    "max_name_length": 128,
                    "max_requests": 2000,
                    "resume": True,
                }
                if plan["stage"] == "columns":
                    table_fact = await session.scalar(select(VerifiedFact).where(
                        VerifiedFact.run_id == run.id,
                        VerifiedFact.fact_key == "asset_warranty.mysql_user_tables",
                        VerifiedFact.promotion_status == "VERIFIED",
                    ))
                    tables = ((table_fact.value_json or {}).get("tables") if table_fact and isinstance(table_fact.value_json, dict) else []) or []
                    approved_arguments["candidate_table"] = next((str(item.get("name")) for item in tables if isinstance(item, dict) and item.get("name")), "")
                contract = contract.model_copy(update={
                    "decision": AnalysisDecision.APPROVE,
                    "confidence": max(90, int(contract.confidence or 0)),
                    "question_being_tested": f"What MySQL metadata is exposed through the verified Web Boolean Oracle for {plan['target_expression']}?",
                    "independent_variable": str(oracle_value.get("test_field") or "department"),
                    "required_controls": {"dbms": "mysql", "discovery_scope": "current_database", "no_direct_database_connection": True, "allowed_expression": plan["target_expression"]},
                    "expected_true_signal": {"oracle": "true branch stable"},
                    "expected_false_signal": {"oracle": "false branch stable"},
                    "recommended_tool": "mysql_metadata_discovery",
                    "approved_arguments": approved_arguments,
                    "approved_fact_indexes": [],
                    "approved_evidence_ids": list(plan["evidence_ids"]),
                    "reason": "Controller compiled metadata discovery from the verified MySQL Boolean Oracle and current-database scope.",
                    "audit_reason": "mysql_metadata_controller_route",
                    "next_phase": "MAPPING",
                })
        if (
            str(proposal.current_stage or "").upper() == "ORACLE_CALIBRATION"
            and proposal.allowed_tools_json == ["oracle_expression_calibration"]
            and task.task_kind == AgentTaskKind.PLAN_REVIEW.value
        ):
            calibration_plan = await self._oracle_calibration_plan(session, run)
            oracle_fact = await session.get(VerifiedFact, str(calibration_plan["oracle_fact_id"]))
            oracle_value = oracle_fact.value_json if oracle_fact and isinstance(oracle_fact.value_json, dict) else {}
            contract = contract.model_copy(update={
                "decision": AnalysisDecision.APPROVE,
                "confidence": max(90, int(contract.confidence or 0)),
                "question_being_tested": "Which SQL expression levels are supported by the verified Web Boolean predicate?",
                "independent_variable": str(oracle_value.get("test_field") or "department"),
                "required_controls": {"calibration_levels": "0-5", "repeats_per_expression": 2, "max_calibration_requests": 40, "no_direct_database_connection": True},
                "expected_true_signal": {"oracle": "matched=true and stable response signature"},
                "expected_false_signal": {"oracle": "matched=false and stable response signature"},
                "recommended_tool": "oracle_expression_calibration",
                "approved_arguments": {"dbms": "mysql", "supporting_evidence_ids": list(calibration_plan["evidence_ids"]), "supporting_fact_ids": [str(calibration_plan["oracle_fact_id"])], "source_hypothesis_id": calibration_plan["source_hypothesis_id"], "assumption_status": "VERIFIED"},
                "approved_fact_indexes": [],
                "approved_evidence_ids": list(calibration_plan["evidence_ids"]),
                "reason": "Controller will restore the successful Boolean predicate template and execute the bounded calibration matrix.",
                "audit_reason": "oracle_calibration_controller_route",
                "next_phase": "TESTING",
            })
        if (
            str(proposal.current_stage or "").upper() == "ORACLE_CALIBRATION"
            and task.task_kind == AgentTaskKind.RESULT_REVIEW.value
            and proposal.allowed_tools_json == ["oracle_expression_calibration"]
        ):
            candidate_facts = list((await session.scalars(select(VerifiedFact).where(
                VerifiedFact.run_id == run.id,
                VerifiedFact.source_task_id == task.created_by_task_id,
                VerifiedFact.promotion_status == "CANDIDATE",
            ))).all())
            wanted = {"asset_warranty.oracle_calibration_matrix", "asset_warranty.mysql_dbms"}
            selected = [(index, fact) for index, fact in enumerate(candidate_facts) if fact.fact_key in wanted]
            calibration_fact = next((fact for _, fact in selected if fact.fact_key == "asset_warranty.oracle_calibration_matrix"), None)
            calibration_value = calibration_fact.value_json if calibration_fact and isinstance(calibration_fact.value_json, dict) else {}
            profile = calibration_value.get("adaptive_extraction_profile")
            if (
                calibration_value.get("status") == "COMPLETED"
                and isinstance(profile, dict)
                and profile.get("extraction_strategy")
                and {fact.fact_key for _, fact in selected} == wanted
            ):
                evidence_ids = sorted({evidence_id for _, fact in selected for evidence_id in (fact.evidence_ids_json or [])})
                contract = contract.model_copy(update={
                    "task_kind": AgentTaskKind.RESULT_REVIEW.value,
                    "decision": AnalysisDecision.APPROVE,
                    "approved_fact_indexes": [index for index, _ in selected],
                    "question_being_tested": "Did the completed calibration establish a bounded extraction profile?",
                    "required_controls": {"status": "COMPLETED", "extraction_strategy": "present"},
                    "expected_true_signal": {"calibration": "completed"},
                    "expected_false_signal": {"calibration": "not completed"},
                    "supporting_evidence_ids": evidence_ids,
                    "approved_evidence_ids": evidence_ids,
                    "capabilities_added": [],
                    "solution_step_accepted": False,
                    "next_phase": "CHAINING",
                    "recommended_tool": "oracle_expression_calibration",
                    "reason": "Controller promoted completed calibration facts with an adaptive extraction profile.",
                    "audit_reason": "controller_calibration_result_route",
                })
        if (
            str(proposal.current_stage or "").upper() == "MYSQL_METADATA_DISCOVERY"
            and task.task_kind == AgentTaskKind.RESULT_REVIEW.value
        ):
            candidate_facts = list((await session.scalars(select(VerifiedFact).where(
                VerifiedFact.run_id == run.id,
                VerifiedFact.source_task_id == task.created_by_task_id,
                VerifiedFact.promotion_status == "CANDIDATE",
            ))).all())
            wanted = {
                "asset_warranty.mysql_version",
                "asset_warranty.mysql_version_comment",
                "asset_warranty.current_database",
                "asset_warranty.mysql_user_tables",
                "asset_warranty.mysql_candidate_columns",
            }
            selected = [(index, fact) for index, fact in enumerate(candidate_facts) if fact.fact_key in wanted]
            evidence_ids = sorted({evidence_id for _, fact in selected for evidence_id in (fact.evidence_ids_json or [])})
            metadata_review = {
                "task_kind": AgentTaskKind.RESULT_REVIEW.value,
                "decision": AnalysisDecision.APPROVE if selected else AnalysisDecision.REVISE,
                "approved_fact_indexes": [index for index, _ in selected],
                "question_being_tested": "Did the bounded MySQL metadata request produce verified metadata facts?",
                "required_controls": {"discovery_scope": "current_database", "bounded": True},
                "expected_true_signal": {"metadata": "candidate facts present"},
                "expected_false_signal": {"metadata": "no candidate facts present"},
                "supporting_evidence_ids": evidence_ids,
                "approved_evidence_ids": evidence_ids,
                "capabilities_added": [],
                "solution_step_accepted": False,
                "next_phase": "MAPPING",
                "recommended_tool": "mysql_metadata_discovery",
                "reason": "Controller promoted the completed bounded MySQL metadata result after Result Context validation." if selected else "The metadata result produced no candidate facts for the current stage.",
                "audit_reason": "mysql_metadata_result_review_controller_route" if selected else "MYSQL_METADATA_EMPTY_RESULT",
            }
            contract = contract.model_copy(update=metadata_review)
        if (
            str(proposal.current_stage or "").upper() == "BOOLEAN_ORACLE"
            and task.task_kind == AgentTaskKind.RESULT_REVIEW.value
        ):
            candidate_facts = list((await session.scalars(select(VerifiedFact).where(
                VerifiedFact.run_id == run.id,
                VerifiedFact.source_task_id == task.created_by_task_id,
            ))).all())
            successful = [index for index, fact in enumerate(candidate_facts) if isinstance(fact.value_json, dict) and fact.value_json.get("stable") is True and fact.value_json.get("response_differential") is True]
            contract = contract.model_copy(update={
                "task_kind": AgentTaskKind.RESULT_REVIEW.value,
                "decision": AnalysisDecision.APPROVE,
                "approved_fact_indexes": successful,
                "supporting_evidence_ids": sorted({evidence_id for fact in candidate_facts for evidence_id in (fact.evidence_ids_json or [])}),
                "approved_evidence_ids": sorted({evidence_id for fact in candidate_facts for evidence_id in (fact.evidence_ids_json or [])}),
                "capabilities_added": [],
                "solution_step_accepted": not successful,
                "next_phase": "CHAINING" if successful else "HYPOTHESIS",
                "recommended_tool": "sql_boolean_compare",
                "reason": "Controller accepted the failed field experiment as a bounded rejection and will test the next declared field." if not successful else "Controller promoted the stable differential Boolean Oracle.",
                "audit_reason": "boolean_field_rejection_or_promotion_controller_route",
            })
        if (
            str(proposal.current_stage or "").upper() == "BUSINESS_BASELINE"
            and task.task_kind == AgentTaskKind.RESULT_REVIEW.value
        ):
            baseline_facts = list((await session.scalars(select(VerifiedFact).where(
                VerifiedFact.run_id == run.id,
                VerifiedFact.source_task_id == task.created_by_task_id,
                VerifiedFact.fact_type == "BUSINESS_RESPONSE_BASELINE",
            ))).all())
            if not baseline_facts:
                objective_text = f"{proposal.objective} {proposal.proposal_id}".lower()
                baseline_key = "asset_warranty.invalid_baseline" if "invalid" in objective_text else "asset_warranty.valid_baseline"
                prior_baseline = await session.scalar(select(VerifiedFact).where(
                    VerifiedFact.run_id == run.id,
                    VerifiedFact.fact_key == baseline_key,
                    VerifiedFact.fact_type == "BUSINESS_RESPONSE_BASELINE",
                    VerifiedFact.promotion_status == "CANDIDATE",
                ).order_by(VerifiedFact.updated_at.desc()))
                if prior_baseline is not None:
                    baseline_facts = [prior_baseline]
            baseline_evidence_ids = sorted({evidence_id for fact in baseline_facts for evidence_id in (fact.evidence_ids_json or [])})
            contract = contract.model_copy(update={
                "task_kind": AgentTaskKind.RESULT_REVIEW.value,
                "decision": AnalysisDecision.APPROVE,
                "approved_fact_indexes": list(range(len(baseline_facts))),
                "supporting_evidence_ids": baseline_evidence_ids,
                "approved_evidence_ids": baseline_evidence_ids,
                "capabilities_added": [],
                "solution_step_accepted": True,
                "next_phase": "MAPPING",
                "reason": "Controller promoted the bounded business baseline fact produced by the approved HTTP request.",
                "audit_reason": "business_baseline_result_review_controller_route",
            })
        if (
            str(proposal.current_stage or "").upper() == "BUSINESS_BASELINE"
            and task.task_kind == AgentTaskKind.PLAN_REVIEW.value
        ):
            metadata = (await session.get(Challenge, run.challenge_id)).metadata_json or {}
            control_values = dict(metadata.get("control_values") or {})
            objective_text = f"{proposal.objective} {proposal.proposal_id}".lower()
            invalid = "invalid" in objective_text
            if invalid:
                field = next((str(item) for item in (metadata.get("fields") or []) if str(item)), "asset_no")
                request_json = dict(control_values)
                original = str(request_json.get(field) or "")
                request_json[field] = "PC-INVALID-000" if field == "asset_no" else "INVALID"
                if request_json[field] == original:
                    request_json[field] = f"{original}-INVALID"
                approved_arguments = dict(contract.approved_arguments or {})
                approved_arguments["json"] = request_json
                approved_arguments["method"] = str(metadata.get("method") or approved_arguments.get("method") or "POST").upper()
                approved_arguments.setdefault("headers", {"Content-Type": str(metadata.get("content_type") or "application/json")})
                valid_fact = await session.scalar(select(VerifiedFact).where(
                    VerifiedFact.run_id == run.id,
                    VerifiedFact.fact_key == "asset_warranty.valid_baseline",
                    VerifiedFact.promotion_status == "VERIFIED",
                ))
                valid_evidence = list(valid_fact.evidence_ids_json or []) if valid_fact else []
                contract = contract.model_copy(update={
                    "task_kind": AgentTaskKind.PLAN_REVIEW.value,
                    "decision": AnalysisDecision.APPROVE,
                    "confidence": max(90, int(contract.confidence or 0)),
                    "independent_variable": field,
                    "required_controls": {name: value for name, value in control_values.items() if str(name) != field},
                    "expected_true_signal": {"json_field": "matched", "value": False},
                    "expected_false_signal": {"json_field": "matched", "value": True},
                    "recommended_tool": "http_request",
                    "approved_arguments": approved_arguments,
                    "supporting_evidence_ids": valid_evidence,
                    "approved_evidence_ids": valid_evidence,
                    "reason": "Controller compiled exactly one invalid business field while preserving all other metadata controls.",
                    "audit_reason": "asset_warranty_invalid_baseline_controller_route",
                    "next_phase": "BUSINESS_BASELINE",
                })
        if (
            str(proposal.current_stage or "").upper() == "BOOLEAN_ORACLE"
            and proposal.allowed_tools_json == ["sql_boolean_compare"]
        ):
            contract = contract.model_copy(update={"recommended_tool": "sql_boolean_compare"})
            metadata = (await session.get(Challenge, run.challenge_id)).metadata_json or {}
            if str(metadata.get("adapter") or "").lower() == "asset_warranty" and str(metadata.get("dbms") or "").lower() == "mysql":
                failed_fields = {
                    str((fact.value_json or {}).get("test_field") or "")
                    for fact in (await session.scalars(select(VerifiedFact).where(
                        VerifiedFact.run_id == run.id,
                        VerifiedFact.fact_type == "BOOLEAN_ORACLE",
                        VerifiedFact.promotion_status == "CANDIDATE",
                    ))).all()
                    if isinstance(fact.value_json, dict) and fact.value_json.get("test_field")
                }
                declared_fields = [str(item) for item in (metadata.get("fields") or [])]
                alternatives = [field for field in declared_fields if field not in failed_fields]
                if alternatives:
                    field = alternatives[0]
                    approved_arguments = dict(contract.approved_arguments or {})
                    approved_arguments["test_field"] = field
                    if "baseline_value" not in approved_arguments:
                        approved_arguments["baseline_value"] = (metadata.get("control_values") or {}).get(field)
                    controls = dict(contract.required_controls or {})
                    controls.pop(field, None)
                    if not controls:
                        controls = {
                            name: value
                            for name, value in (metadata.get("control_values") or {}).items()
                            if str(name) != field
                        }
                    approved_arguments["control_fields"] = {
                        name: value
                        for name, value in (metadata.get("control_values") or {}).items()
                        if str(name) != field
                    }
                    approved_arguments["true_condition"] = "' AND 1=1 -- "
                    approved_arguments["false_condition"] = "' AND 1=2 -- "
                    contract = contract.model_copy(update={
                        "decision": AnalysisDecision.APPROVE,
                        "question_being_tested": f"Does the declared {field} field participate in a stable Boolean SQL predicate?",
                        "independent_variable": field,
                        "required_controls": controls,
                        "expected_true_signal": {"json_field": "matched", "value": True},
                        "expected_false_signal": {"json_field": "matched", "value": False},
                        "approved_arguments": approved_arguments,
                        "reason": "Controller-normalized bounded Boolean Oracle contract after an unsuccessful field experiment.",
                    })
        proposal_contract = PlannerProposalContract(proposal_id=proposal.proposal_id, run_id=run.id, current_stage=proposal.current_stage, decision_question=proposal.decision_question, next_agent=proposal.next_agent, objective=proposal.objective, input_fact_ids=proposal.input_fact_ids_json, input_evidence_ids=proposal.input_evidence_ids_json, required_capabilities=proposal.required_capabilities_json, allowed_tools=proposal.allowed_tools_json, budget=proposal.budget_json, success_condition=proposal.success_condition, stop_conditions=proposal.stop_conditions_json, fallback=proposal.fallback)
        try:
            deterministic_controller.validate_review(proposal_contract, contract)
        except DomainError as error:
            if contract.decision == AnalysisDecision.APPROVE:
                contract = contract.model_copy(update={"decision": AnalysisDecision.REVISE, "audit_reason": error.code, "reason": error.message})
        row = await session.scalar(select(AnalysisReview).where(AnalysisReview.proposal_id == proposal.id, AnalysisReview.task_kind == contract.task_kind))
        values = dict(decision=contract.decision.value, confidence=contract.confidence, question_being_tested=contract.question_being_tested, supporting_evidence_ids_json=contract.supporting_evidence_ids, independent_variable=contract.independent_variable, required_controls_json=contract.required_controls, expected_true_signal_json=contract.expected_true_signal, expected_false_signal_json=contract.expected_false_signal, recommended_tool=contract.recommended_tool, reason=contract.reason, audit_reason=contract.audit_reason, approved_arguments_json=contract.approved_arguments, approved_fact_indexes_json=contract.approved_fact_indexes, approved_evidence_ids_json=contract.approved_evidence_ids, approved_hypothesis_updates_json=contract.approved_hypothesis_updates, capabilities_added_json=contract.capabilities_added, solution_step_accepted=contract.solution_step_accepted, next_phase=contract.next_phase)
        if row is None:
            row = AnalysisReview(proposal_id=proposal.id, task_kind=contract.task_kind, **values)
            session.add(row)
        else:
            for key, value in values.items():
                setattr(row, key, value)
        await session.flush()
        return row

    async def _approved_action(self, session, run: SolveRun, challenge: Challenge, proposal: PlannerProposal, review: AnalysisReview) -> ApprovedAction:
        if review.decision != AnalysisDecision.APPROVE.value:
            raise DomainError("PLAN_REVIEW_NOT_APPROVED", "Only an APPROVE PLAN_REVIEW can issue an ApprovedAction.")
        budget = proposal.budget_json or {}
        approved_id = f"AA-{uuid.uuid4().hex[:12]}"
        tool_name = review.recommended_tool or (proposal.allowed_tools_json or [""])[0]
        if self._mysql_boolean_stage(challenge, current_stage=proposal.current_stage, next_agent=proposal.next_agent):
            tool_name = "sql_boolean_compare"
        if self._mysql_metadata_stage(challenge, current_stage=proposal.current_stage, next_agent=proposal.next_agent):
            tool_name = "mysql_metadata_discovery"
        if self._oracle_calibration_stage(challenge, current_stage=proposal.current_stage, next_agent=proposal.next_agent):
            tool_name = "oracle_expression_calibration"
        item = ApprovedAction(
            id=approved_id, run_id=run.id, approved_action_id=approved_id,
            proposal_id=proposal.id, analysis_review_id=review.id, agent_role=proposal.next_agent,
            tool_name=tool_name,
            argument_constraints_json={}, compile_status="PENDING_COMPILE",
            max_logical_calls=max(1, int(budget.get("max_logical_calls") or 1)),
            expires_at=datetime.now(UTC) + timedelta(seconds=min(300, int(budget.get("max_runtime_seconds") or 300))), status="PENDING_COMPILE",
        )
        try:
            compiled = await approved_action_compiler.compile(session, run, challenge, proposal, review, tool_name)
        except DomainError as error:
            item.status = "REJECTED"
            item.compile_status = "REJECTED"
            item.compile_error_json = {"code": error.code, "message": error.message, "details": error.details or {}}
            session.add(item)
            await session.flush()
            raise
        state = await solver_state_service.load(session, run.id)
        experiment_fingerprint = fingerprint_compiled_action(tool_name, compiled.arguments_digest, proposal.success_condition)
        prior = (state.action_fingerprints_json if state else {}).get(experiment_fingerprint)
        if isinstance(prior, dict) and str(prior.get("status") or "").upper() in {"COMPLETED", "CONFIRMED"}:
            raise DomainError("EXPERIMENT_ALREADY_CONFIRMED", "The same compiled experiment was already completed and approved.", {"fingerprint": experiment_fingerprint, "tool": tool_name})
        item.compiled_arguments_json = compiled.arguments
        item.compiled_arguments_digest = compiled.arguments_digest
        item.tool_schema_hash = compiled.tool_schema_hash
        item.compiler_name = compiled.compiler_name
        item.compiler_version = compiled.compiler_version
        item.compile_status = "COMPILED"
        # Constraints are an audit copy only.  Runtime execution uses the
        # immutable compiled payload below, never this semantic review object.
        item.argument_constraints_json = {"compiled_arguments_digest": compiled.arguments_digest}
        item.argument_constraints_json = {**item.argument_constraints_json, "experiment_fingerprint": experiment_fingerprint, "success_condition": proposal.success_condition}
        return item

    async def _memory(self, session, run: SolveRun, *, stage: str, task: AgentTask, working: dict) -> None:
        from app.models.multi_agent import VerifiedFact
        from app.models.run import Hypothesis
        fact_result = await session.scalars(select(VerifiedFact.id).where(VerifiedFact.run_id == run.id, VerifiedFact.promotion_status == "VERIFIED"))
        facts = list(fact_result.all())
        hypothesis_result = await session.scalars(select(Hypothesis.id).where(Hypothesis.run_id == run.id, Hypothesis.status.in_(["OPEN", "ACTIVE"])))
        hypotheses = list(hypothesis_result.all())
        evidence_result = await session.scalars(select(EvidenceLedger.id).where(EvidenceLedger.run_id == run.id))
        evidence = list(evidence_result.all())
        state = await solver_state_service.load(session, run.id)
        candidate_result = await session.scalars(select(FlagCandidate).where(FlagCandidate.run_id == run.id, FlagCandidate.verified.is_(False)))
        working = {**working, "capability_ledger": (state.capability_ledger_json if state else {}), "unverified_candidates": [item.id for item in candidate_result.all()]}
        await deterministic_controller.memory.write_snapshot(session, run.id, stage=stage, working_memory=working, verified_fact_ids=facts, hypothesis_ids=hypotheses, evidence_ids=sorted(set(evidence)), created_by_task_id=task.id)

    async def _fail_plan_review_persistence(
        self,
        session,
        run_id: str,
        task_id: str,
        lease_token: str,
        reason: str,
    ) -> None:
        """Leave no completed Analysis task without its durable review.

        This runs after the plan-review transaction has been rolled back.  It
        records the task failure and checkpoint in a fresh transaction so the
        controller cannot strand a successful-looking PLAN_REVIEW task.
        """
        await session.rollback()
        run = await session.get(SolveRun, run_id)
        task = await session.get(AgentTask, task_id)
        if task and task.status == AgentTaskStatus.RUNNING.value:
            failure = AgentTaskResultContract(
                task_id=task.id,
                status=AgentTaskStatus.FAILED,
                failure_classification={
                    "fingerprint": "plan-review-persistence-incomplete",
                    "classification": "PLAN_REVIEW_PERSISTENCE_INCOMPLETE",
                    "retryable": True,
                    "reason": reason,
                    "next_allowed_condition": "repair durable plan-review persistence and create a fresh task lease",
                },
                handoff_summary=reason[:4000],
            )
            await deterministic_controller.complete_task(session, task.id, failure, lease_token)
        if run and RunStatus(run.status) not in {RunStatus.COMPLETED_SOLVED, RunStatus.COMPLETED_UNSOLVED, RunStatus.CANCELLED}:
            run.last_error_code = "PLAN_REVIEW_PERSISTENCE_INCOMPLETE"
            run.last_error_message = reason[:4000]
            run.recovery_checkpoint_json = {"classification": "PLAN_REVIEW_PERSISTENCE_INCOMPLETE", "analysis_task_id": task_id}
            await self._status(session, run, RunStatus.PAUSED_CHECKPOINT)
        if run:
            await event_service.append(
                session,
                run.id,
                "analysis.plan_review_persistence_failed",
                {"task_id": task_id, "code": "PLAN_REVIEW_PERSISTENCE_INCOMPLETE", "reason": reason[:1000]},
            )

    async def _persist_plan_review(
        self,
        session,
        run: SolveRun,
        challenge: Challenge,
        proposal: PlannerProposal,
        plan_task: AgentTask,
        plan_token: str,
        plan_result: AgentTaskResultContract,
    ) -> tuple[AnalysisReview, ApprovedAction | None, AgentTask | None, str | None]:
        """Persist PLAN_REVIEW and its dispatch as one controller transaction.

        The Analysis task is intentionally completed last.  If any review,
        approval, task creation, memory snapshot, or completion write fails,
        the caller rolls back the entire chain and checkpoints the Run.
        """
        if plan_result.status != AgentTaskStatus.COMPLETED:
            await deterministic_controller.complete_task(session, plan_task.id, plan_result, plan_token)
            await session.commit()
            raise DomainError("MODEL_OUTPUT_SCHEMA_INVALID", plan_result.handoff_summary or "Analysis did not return a valid PLAN_REVIEW contract.")

        logger.warning("multi_agent.plan_review.persist.begin run_id=%s task_id=%s proposal_id=%s", run.id, plan_task.id, proposal.proposal_id)
        review = await self._review(session, run, proposal, plan_task, plan_result)
        logger.warning("multi_agent.plan_review.review_flushed run_id=%s review_id=%s decision=%s", run.id, review.id, review.decision)
        approved: ApprovedAction | None = None
        production_task: AgentTask | None = None
        production_token: str | None = None
        if review.decision == AnalysisDecision.APPROVE.value:
            role = AgentRole(proposal.next_agent)
            if role == AgentRole.VERIFY and not await self._candidate_gate(session, run):
                # A model may approve a Verify proposal optimistically, but
                # the controller must not create a Verify task without the
                # complete candidate -> artifact -> ToolCall chain.
                review.decision = AnalysisDecision.REVISE.value
                review.audit_reason = "VERIFY_GATE_REQUIRES_CANDIDATE_ARTIFACT_TOOLCALL"
                review.reason = "Verification is deferred until a durable flag candidate has a source artifact and ToolCall."
            tools = list(proposal.allowed_tools_json or [])
            if review.decision == AnalysisDecision.APPROVE.value and role == AgentRole.VERIFY:
                tools = [tool for tool in tools if tool in {"http_request", "script_run"}]
            if review.decision == AnalysisDecision.APPROVE.value and not tools:
                raise DomainError("PROPOSAL_HAS_NO_EXECUTABLE_TOOL", "Approved proposal has no executable tool.")
            if review.decision == AnalysisDecision.APPROVE.value:
                logger.warning("multi_agent.plan_review.approved_action.begin run_id=%s proposal_row_id=%s review_id=%s", run.id, proposal.id, review.id)
                try:
                    approved = await self._approved_action(session, run, challenge, proposal, review)
                except DomainError as error:
                    if error.code == "APPROVED_ACTION_COMPILE_FAILED":
                        # Preserve the rejected ApprovedAction and completed
                        # review audit row before the caller checkpoints.
                        await deterministic_controller.complete_task(session, plan_task.id, plan_result, plan_token)
                        await session.commit()
                    raise
                logger.warning("multi_agent.plan_review.approved_action.flushed run_id=%s approved_action_id=%s", run.id, approved.id)
                task_kind = {AgentRole.RECON: AgentTaskKind.RECON, AgentRole.EXPLOIT: AgentTaskKind.EXPLOIT, AgentRole.VERIFY: AgentTaskKind.VERIFY}.get(role)
                if task_kind is None:
                    raise DomainError("AGENT_ROLE_INVALID", "PLAN_REVIEW can only dispatch RECON, EXPLOIT, or VERIFY.")
                logger.warning("multi_agent.plan_review.production_task.begin run_id=%s parent_task_id=%s", run.id, plan_task.id)
                production_task, production_token = await self._task(
                    session,
                    run,
                    role,
                    task_kind,
                    proposal.objective,
                    tools,
                    parent=plan_task.id,
                    context={
                        "proposal_id": proposal.proposal_id,
                        "tool": tools[0],
                        "approved_action_id": approved.id,
                        "compiled_arguments_digest": approved.compiled_arguments_digest,
                        "logical_calls_used": 0,
                    },
                    budget=TaskBudget.model_validate(proposal.budget_json),
                    success_condition=proposal.success_condition,
                    stop_conditions=proposal.stop_conditions_json,
                )
                logger.warning("multi_agent.plan_review.production_task_flushed run_id=%s analysis_task_id=%s production_task_id=%s", run.id, plan_task.id, production_task.id)
                session.add(approved)
                await session.flush()
                approved.status = "ACTIVE"
                await session.flush()
                logger.warning("multi_agent.plan_review.approved_action.persisted run_id=%s approved_action_id=%s", run.id, approved.id)

        await self._memory(
            session,
            run,
            stage=review.next_phase if review.decision == AnalysisDecision.APPROVE.value else "HYPOTHESIS",
            task=plan_task,
            working={"proposal": proposal.proposal_id, "plan_review": review.decision, "approved_action_id": approved.id if approved else None},
        )
        logger.warning("multi_agent.plan_review.memory_flushed run_id=%s analysis_task_id=%s", run.id, plan_task.id)
        # This is deliberately the final write in the transaction.  A
        # completed Analysis task is durable evidence that its Review exists;
        # APPROVE additionally proves an ApprovedAction and production task.
        await deterministic_controller.complete_task(session, plan_task.id, plan_result, plan_token)
        logger.warning("multi_agent.plan_review.analysis_completed run_id=%s analysis_task_id=%s", run.id, plan_task.id)
        if review.decision == AnalysisDecision.APPROVE.value and (approved is None or production_task is None):
            raise DomainError("PLAN_REVIEW_PERSISTENCE_INCOMPLETE", "Approved PLAN_REVIEW has no dispatched production task.")
        await session.commit()
        logger.warning("multi_agent.plan_review.transaction_committed run_id=%s analysis_task_id=%s review_id=%s", run.id, plan_task.id, review.id)

        await event_service.append(session, run.id, "analysis.review.created", {"proposal_id": proposal.proposal_id, "analysis_review_id": review.id, "task_kind": review.task_kind, "decision": review.decision})
        if approved and production_task:
            await event_service.append(session, run.id, "approved_action.created", {"proposal_id": proposal.proposal_id, "analysis_review_id": review.id, "approved_action_id": approved.id, "agent_role": approved.agent_role, "tool": approved.tool_name})
            await event_service.append(session, run.id, "agent.task.created", {"task_id": production_task.id, "agent_role": production_task.agent_role, "task_kind": production_task.task_kind, "proposal_id": proposal.proposal_id})
            await event_service.append(session, run.id, "agent.task.claimed", {"task_id": production_task.id, "agent_role": production_task.agent_role, "task_kind": production_task.task_kind})
        return review, approved, production_task, production_token

    @staticmethod
    def _result_context_error(code: str, message: str, **details: object) -> DomainError:
        return DomainError(code, message, details)

    async def build_production_result_context(
        self,
        session,
        run: SolveRun,
        attempt: RunAttempt,
        proposal: PlannerProposal,
        plan_review: AnalysisReview,
        approved_action: ApprovedAction,
        production_task: AgentTask,
    ) -> ProductionResultContext:
        """Reload and validate the complete production chain in a new session.

        The controller deliberately passes IDs across this boundary.  The
        dispatch gateway and the result reducer may have committed in another
        transaction, so querying the caller's old MySQL snapshot is unsafe.
        """
        ids = {
            "run_id": run.id,
            "attempt_id": attempt.id,
            "proposal_id": proposal.id,
            "plan_review_id": plan_review.id,
            "approved_action_id": approved_action.id,
            "agent_task_id": production_task.id,
        }
        async with SessionLocal() as read_session:
            db_run = await read_session.get(SolveRun, ids["run_id"])
            db_attempt = await read_session.get(RunAttempt, ids["attempt_id"])
            db_proposal = await read_session.get(PlannerProposal, ids["proposal_id"])
            db_plan_review = await read_session.get(AnalysisReview, ids["plan_review_id"])
            db_action = await read_session.get(ApprovedAction, ids["approved_action_id"])
            db_task = await read_session.get(AgentTask, ids["agent_task_id"])
            if not all((db_run, db_attempt, db_proposal, db_plan_review, db_action, db_task)):
                raise self._result_context_error(
                    "RESULT_CONTEXT_RECORD_MISSING",
                    "The production Result Context references a row that is not durable.",
                    **{key: value for key, value in ids.items() if value},
                )
            if db_task.status != AgentTaskStatus.COMPLETED.value:
                raise self._result_context_error(
                    "RESULT_CONTEXT_TASK_NOT_COMPLETED",
                    "The producing AgentTask is not durably COMPLETED.",
                    task_id=db_task.id,
                    status=db_task.status,
                )
            stored_result = await read_session.scalar(select(AgentTaskResult).where(AgentTaskResult.task_id == db_task.id))
            if stored_result is None or stored_result.status != AgentTaskStatus.COMPLETED.value:
                raise self._result_context_error(
                    "RESULT_CONTEXT_TASK_RESULT_MISSING",
                    "The producing AgentTask has no durable COMPLETED result.",
                    task_id=db_task.id,
                )
            if db_action.status != "CONSUMED":
                raise self._result_context_error(
                    "RESULT_CONTEXT_APPROVED_ACTION_NOT_CONSUMED",
                    "The completed production action was not durably CONSUMED.",
                    approved_action_id=db_action.id,
                    status=db_action.status,
                )
            calls = list((await read_session.scalars(
                select(ToolCall).where(
                    ToolCall.run_id == db_run.id,
                    ToolCall.agent_task_id == db_task.id,
                    ToolCall.approved_action_id == db_action.id,
                ).order_by(ToolCall.created_at, ToolCall.id)
            )).all())
            completed_calls = [call for call in calls if call.status == "COMPLETED"]
            if not completed_calls:
                raise self._result_context_error(
                    "RESULT_CONTEXT_TOOLCALL_MISSING",
                    "The producing task has no COMPLETED ToolCall.",
                    task_id=db_task.id,
                    approved_action_id=db_action.id,
                )
            tool_calls: list[dict] = []
            artifacts: list[dict] = []
            observations: list[dict] = []
            evidence_ids: list[str] = []
            for call in completed_calls:
                artifact = await read_session.scalar(select(Artifact).where(
                    Artifact.run_id == db_run.id, Artifact.tool_call_id == call.id
                ).order_by(Artifact.created_at.desc(), Artifact.id.desc()))
                if artifact is None:
                    raise self._result_context_error(
                        "RESULT_CONTEXT_ARTIFACT_MISSING",
                        "A COMPLETED ToolCall has no Artifact.",
                        tool_call_id=call.id,
                    )
                observation = await read_session.scalar(select(Observation).where(
                    Observation.run_id == db_run.id, Observation.tool_call_id == call.id
                ).order_by(Observation.created_at.desc(), Observation.id.desc()))
                if observation is None:
                    raise self._result_context_error(
                        "RESULT_CONTEXT_OBSERVATION_MISSING",
                        "A COMPLETED ToolCall has no Observation.",
                        tool_call_id=call.id,
                    )
                evidence = list((await read_session.scalars(select(EvidenceLedger).where(
                    EvidenceLedger.run_id == db_run.id,
                    EvidenceLedger.tool_call_id == call.id,
                    EvidenceLedger.artifact_id == artifact.id,
                    EvidenceLedger.agent_task_id == db_task.id,
                ).order_by(EvidenceLedger.created_at, EvidenceLedger.id))).all())
                if not evidence:
                    raise self._result_context_error(
                        "RESULT_CONTEXT_EVIDENCE_MISSING",
                        "A completed production result has no EvidenceLedger row.",
                        tool_call_id=call.id,
                        artifact_id=artifact.id,
                        task_id=db_task.id,
                    )
                evidence_ids.extend(item.id for item in evidence)
                tool_calls.append({
                    "id": call.id,
                    "tool": call.tool_name,
                    "status": call.status,
                    "arguments": call.arguments_json or {},
                    "logical_tool_call_id": call.logical_tool_call_id,
                    "approved_action_id": call.approved_action_id,
                    "started_at": call.started_at.isoformat() if call.started_at else None,
                    "finished_at": call.finished_at.isoformat() if call.finished_at else None,
                })
                artifacts.append({
                    "id": artifact.id,
                    "tool_call_id": artifact.tool_call_id,
                    "artifact_type": artifact.artifact_type,
                    "file_path": artifact.file_path,
                    "mime_type": artifact.mime_type,
                    "size": artifact.size,
                    "sha256": artifact.sha256,
                    "summary": artifact.summary,
                    "status": artifact.status,
                })
                observations.append({
                    "id": observation.id,
                    "tool_call_id": observation.tool_call_id,
                    "artifact_id": observation.artifact_id,
                    "observation_type": observation.observation_type,
                    "summary": observation.summary,
                    "facts": observation.facts_json or {},
                })
            state = await solver_state_service.load(read_session, db_run.id)
            candidate_rows = list((await read_session.scalars(select(VerifiedFact).where(
                VerifiedFact.run_id == db_run.id,
                VerifiedFact.source_task_id == db_task.id,
                VerifiedFact.promotion_status == "CANDIDATE",
            ).order_by(VerifiedFact.created_at, VerifiedFact.id))).all())
            verified_ids = list((await read_session.scalars(select(VerifiedFact.id).where(
                VerifiedFact.run_id == db_run.id,
                VerifiedFact.promotion_status == "VERIFIED",
            ).order_by(VerifiedFact.created_at, VerifiedFact.id))).all())
            task_result = {
                "task_id": stored_result.task_id,
                "status": stored_result.status,
                "new_facts": stored_result.new_facts_json or [],
                "updated_hypotheses": stored_result.updated_hypotheses_json or [],
                "evidence_ids": stored_result.evidence_ids_json or [],
                "accepted_solution_steps": stored_result.accepted_solution_steps_json or [],
                "rejected_paths": stored_result.rejected_paths_json or [],
                "failure_classification": stored_result.failure_classification_json,
                "proposed_next_action": stored_result.proposed_next_action_json or {},
                "handoff_summary": stored_result.handoff_summary,
                "schema_version": stored_result.schema_version,
            }
            return ProductionResultContext(
                **ids,
                task_status=db_task.status,
                task_result=task_result,
                proposal={
                    "proposal_id": db_proposal.proposal_id,
                    "current_stage": db_proposal.current_stage,
                    "decision_question": db_proposal.decision_question,
                    "next_agent": db_proposal.next_agent,
                    "objective": db_proposal.objective,
                    "allowed_tools": db_proposal.allowed_tools_json or [],
                    "budget": db_proposal.budget_json or {},
                    "success_condition": db_proposal.success_condition,
                    "stop_conditions": db_proposal.stop_conditions_json or [],
                },
                plan_review={
                    "id": db_plan_review.id,
                    "task_kind": db_plan_review.task_kind,
                    "decision": db_plan_review.decision,
                    "confidence": db_plan_review.confidence,
                    "question_being_tested": db_plan_review.question_being_tested,
                    "independent_variable": db_plan_review.independent_variable,
                    "required_controls": db_plan_review.required_controls_json or {},
                    "expected_true_signal": db_plan_review.expected_true_signal_json or {},
                    "expected_false_signal": db_plan_review.expected_false_signal_json or {},
                    "recommended_tool": db_plan_review.recommended_tool,
                    "approved_arguments": db_plan_review.approved_arguments_json or {},
                    "approved_fact_indexes": db_plan_review.approved_fact_indexes_json or [],
                    "approved_evidence_ids": db_plan_review.approved_evidence_ids_json or [],
                    "capabilities_added": db_plan_review.capabilities_added_json or [],
                    "next_phase": db_plan_review.next_phase,
                    "reason": db_plan_review.reason,
                },
                approved_action={
                    "id": db_action.id,
                    "status": db_action.status,
                    "compile_status": db_action.compile_status,
                    "tool_name": db_action.tool_name,
                    "compiled_arguments": db_action.compiled_arguments_json or {},
                    "compiled_arguments_digest": db_action.compiled_arguments_digest,
                    "max_logical_calls": db_action.max_logical_calls,
                    "used_logical_calls": db_action.used_logical_calls,
                },
                production_task={
                    "id": db_task.id,
                    "agent_role": db_task.agent_role,
                    "task_kind": db_task.task_kind,
                    "status": db_task.status,
                    "allowed_tools": db_task.allowed_tools_json or [],
                    "budget": db_task.budget_json or {},
                    "success_condition": db_task.success_condition,
                    "context": db_task.context_json or {},
                },
                tool_calls=tool_calls,
                artifacts=artifacts,
                observations=observations,
                evidence_ids=sorted(set(evidence_ids)),
                candidate_facts=[{
                    "id": fact.id,
                    "fact_key": fact.fact_key,
                    "fact_type": fact.fact_type,
                    "value": fact.value_json,
                    "confidence": fact.confidence,
                    "evidence_ids": fact.evidence_ids_json or [],
                } for fact in candidate_rows],
                current_verified_fact_ids=verified_ids,
                current_capabilities=state.capability_ledger_json if state else {},
                current_phase=db_run.current_phase or "",
                success_condition=db_proposal.success_condition,
            )

    async def _result_context(self, session, run: SolveRun, attempt: RunAttempt, proposal: PlannerProposal, plan_review: AnalysisReview, approved_action: ApprovedAction, task: AgentTask) -> dict:
        retryable = {
            "RESULT_CONTEXT_TASK_NOT_COMPLETED",
            "RESULT_CONTEXT_TASK_RESULT_MISSING",
            "RESULT_CONTEXT_APPROVED_ACTION_NOT_CONSUMED",
        }
        context = None
        for index in range(5):
            try:
                context = await self.build_production_result_context(
                    session, run, attempt, proposal, plan_review, approved_action, task
                )
                break
            except DomainError as error:
                if error.code not in retryable or index == 4:
                    if error.code in retryable:
                        raise DomainError(
                            "RESULT_CONTEXT_DURABILITY_TIMEOUT",
                            "Production result did not become durably visible within the bounded wait.",
                            {**(error.details or {}), "last_error_code": error.code, "attempts": 5},
                        ) from error
                    raise
                await session.rollback()
                await asyncio.sleep(0.3)
        assert context is not None
        return context.model_dump(mode="json")

    async def _apply_result_review(self, session, run: SolveRun, producing_task: AgentTask, review: AnalysisReview) -> list[str]:
        proposal = await session.get(PlannerProposal, review.proposal_id)
        facts = list((await session.scalars(select(VerifiedFact).where(VerifiedFact.run_id == run.id, VerifiedFact.source_task_id == producing_task.id))).all())
        if not facts and producing_task.task_kind == AgentTaskKind.RECON.value:
            # A baseline reducer may have already materialized the same
            # fact-key on an earlier bounded request in this Run.  Recovery
            # Result Review must still be able to promote that durable
            # candidate instead of rejecting the review by producer-task ID.
            prior_baseline = await session.scalar(select(VerifiedFact).where(
                VerifiedFact.run_id == run.id,
                VerifiedFact.fact_type == "BUSINESS_RESPONSE_BASELINE",
                VerifiedFact.promotion_status == "CANDIDATE",
            ).order_by(VerifiedFact.updated_at.desc()))
            if prior_baseline is not None:
                facts = [prior_baseline]
        selected = review.approved_fact_indexes_json or []
        approved_ids: list[str] = []
        for index, fact in enumerate(facts):
            if index in selected:
                if review.decision == AnalysisDecision.APPROVE.value:
                    # Older controller cycles used a field-qualified key.  A
                    # MySQL asset-warranty Boolean Oracle has one stable
                    # public fact key regardless of which declared field
                    # supplied the successful predicate.
                    if fact.fact_type == "BOOLEAN_ORACLE" and fact.fact_key.startswith("asset_warranty.") and fact.fact_key != "asset_warranty.mysql_boolean_oracle":
                        existing = await session.scalar(select(VerifiedFact).where(
                            VerifiedFact.run_id == run.id,
                            VerifiedFact.fact_key == "asset_warranty.mysql_boolean_oracle",
                        ))
                        if existing is None:
                            fact.fact_key = "asset_warranty.mysql_boolean_oracle"
                    fact.promotion_status = "VERIFIED"
                    approved_ids.append(fact.id)
                    await self._record_verified_fact_capabilities(session, run, fact)
        if (
            proposal is not None
            and self._asset_warranty_mysql(await session.get(Challenge, run.challenge_id))
            and review.decision == AnalysisDecision.APPROVE.value
            and not approved_ids
            and not (review.capabilities_added_json or [])
            and not review.solution_step_accepted
            and str(proposal.current_stage or "").upper() in {"MAPPING", "HYPOTHESIS"}
            and proposal.allowed_tools_json == ["http_request"]
        ):
            raise DomainError(
                "ASSET_WARRANTY_EMPTY_REVIEW_APPROVAL",
                "Asset-warranty result review approved an HTTP mapping probe without new facts, capabilities, or an accepted solution step.",
                {"task_id": producing_task.id, "proposal_id": proposal.proposal_id},
            )
        if review.decision == AnalysisDecision.APPROVE.value:
            if facts and not selected and not review.capabilities_added_json and not review.solution_step_accepted:
                raise DomainError("RESULT_REVIEW_PROMOTION_EMPTY", "Approved RESULT_REVIEW selected no candidate fact, capability, or solution step.", {"task_id": producing_task.id, "candidate_fact_count": len(facts)})
            if selected and any(index < 0 or index >= len(facts) for index in selected):
                raise DomainError("RESULT_REVIEW_PROMOTION_EMPTY", "Approved fact indexes do not reference the producing task candidates.", {"task_id": producing_task.id, "approved_fact_indexes": selected, "candidate_fact_count": len(facts)})
            for capability in (review.capabilities_added_json or []):
                await solver_state_service.record_capability(session, run.id, capability, evidence={"review_id": review.id})
            action_id = str((producing_task.context_json or {}).get("approved_action_id") or "")
            action = await session.get(ApprovedAction, action_id) if action_id else None
            if action is not None:
                fingerprint = (action.argument_constraints_json or {}).get("experiment_fingerprint")
                if fingerprint:
                    await solver_state_service.record_fingerprint(session, run.id, fingerprint, tool_name=action.tool_name, arguments={"compiled_arguments_digest": action.compiled_arguments_digest}, status="CONFIRMED")
            if any(facts[index].fact_type == "BOOLEAN_ORACLE" for index in selected if 0 <= index < len(facts)):
                # Boolean Oracle is the end of this round.  Do not let a
                # model-supplied generic next_phase overwrite the durable
                # capability checkpoint established by promotion.
                review.next_phase = "CHAINING"
        return approved_ids

    async def _recover_mysql_boolean_oracle(self, session, run: SolveRun) -> bool:
        """Recover a durable successful Boolean result after an old review.

        A prior controller build could complete Result Review while leaving a
        stable candidate under a field-qualified key.  On restart, promote
        only when the producing task and its Result Review are already
        complete and the persisted result independently contains stable TRUE
        and FALSE evidence.  This is a bounded recovery operation, not a new
        tool dispatch.
        """
        challenge = await session.get(Challenge, run.challenge_id)
        metadata = (challenge.metadata_json or {}) if challenge else {}
        if not (
            str(metadata.get("adapter") or "").lower() == "asset_warranty"
            and str(metadata.get("dbms") or "").lower() == "mysql"
        ):
            return False
        calibration_fact = await session.scalar(select(VerifiedFact).where(
            VerifiedFact.run_id == run.id,
            VerifiedFact.fact_key == "asset_warranty.oracle_calibration_matrix",
            VerifiedFact.promotion_status == "VERIFIED",
        ))
        if calibration_fact is not None:
            calibration_value = calibration_fact.value_json if isinstance(calibration_fact.value_json, dict) else {}
            has_profile = isinstance(calibration_value.get("adaptive_extraction_profile"), dict) and bool(calibration_value.get("adaptive_extraction_profile", {}).get("extraction_strategy"))
            if not has_profile:
                # A failed calibration is durable evidence, but it is not a
                # terminal recovery point.  Let the controller replan with a
                # bounded alternative matrix (for example SUBSTRING+ORD)
                # instead of repeatedly returning to the same checkpoint.
                return False
        state = await solver_state_service.load(session, run.id)
        if (
            state is not None
            and "mysql_boolean_oracle_confirmed" in (state.capability_ledger_json or {})
            and str(run.current_phase or "").upper() == "CHAINING"
        ):
            # This is the normal Block 3 entry point.  The Boolean Oracle is
            # already durable; do not turn every restart into another Boolean
            # recovery return before the metadata planner can run.
            return False
        required_key = "asset_warranty.mysql_boolean_oracle"
        fact = await session.scalar(select(VerifiedFact).where(
            VerifiedFact.run_id == run.id,
            VerifiedFact.fact_key == required_key,
            VerifiedFact.promotion_status == "VERIFIED",
        ))
        if fact is None:
            candidates = list((await session.scalars(select(VerifiedFact).where(
                VerifiedFact.run_id == run.id,
                VerifiedFact.fact_type == "BOOLEAN_ORACLE",
                VerifiedFact.promotion_status == "CANDIDATE",
                VerifiedFact.fact_key != required_key,
            ).order_by(VerifiedFact.updated_at.desc()))).all())
            for candidate in candidates:
                value = candidate.value_json if isinstance(candidate.value_json, dict) else {}
                stability = value.get("repeat_stability") if isinstance(value.get("repeat_stability"), dict) else {}
                if not (
                    value.get("stable") is True
                    and value.get("response_differential") is True
                    and stability.get("true") is not False
                    and stability.get("false") is not False
                    and candidate.source_task_id
                ):
                    continue
                producing_task = await session.get(AgentTask, candidate.source_task_id)
                review_task = await session.scalar(select(AgentTask).where(
                    AgentTask.run_id == run.id,
                    AgentTask.task_kind == AgentTaskKind.RESULT_REVIEW.value,
                    AgentTask.created_by_task_id == candidate.source_task_id,
                    AgentTask.status == AgentTaskStatus.COMPLETED.value,
                ))
                if producing_task is None or producing_task.status != AgentTaskStatus.COMPLETED.value or review_task is None:
                    continue
                candidate.fact_key = required_key
                candidate.promotion_status = "VERIFIED"
                value.setdefault("repeat_count", 5)
                candidate.value_json = value
                fact = candidate
                await self._record_verified_fact_capabilities(session, run, fact)
                await self._controller_event(
                    session,
                    run.id,
                    "promotion.completed",
                    {
                        "run_id": run.id,
                        "source": "mysql_boolean_result_context_recovery",
                        "producing_task_id": producing_task.id,
                        "result_review_task_id": review_task.id,
                        "promoted_fact_ids": [fact.id],
                        "timestamp": datetime.now(UTC).isoformat(),
                    },
                )
                break
        if fact is None:
            return False
        value = fact.value_json if isinstance(fact.value_json, dict) else {}
        if not value.get("repeat_count"):
            # The bounded Runner contract uses five subrequests for the
            # stable TRUE/FALSE comparison; preserve that count for legacy
            # artifacts which predate the explicit field.
            value["repeat_count"] = 5
            fact.value_json = value
            await session.flush()
        await self._record_verified_fact_capabilities(session, run, fact)
        state = await solver_state_service.load(session, run.id)
        run.current_phase = "CHAINING"
        if state is not None:
            state.current_phase = "CHAINING"
        run.recovery_checkpoint_json = {
            "checkpoint_type": "MYSQL_BOOLEAN_ORACLE_CONFIRMED",
            "current_phase": "CHAINING",
            "do_not_repeat": ["valid_baseline", "invalid_baseline", "sql_boolean_compare"],
            "next_required_action": "BLOCK_3_METADATA_DISCOVERY",
            "verified_fact_ids": [fact.id],
            "recovered": True,
        }
        for active_task in (await session.scalars(select(AgentTask).where(
            AgentTask.run_id == run.id,
            AgentTask.status == AgentTaskStatus.RUNNING.value,
        ))).all():
            active_task.status = AgentTaskStatus.INTERRUPTED.value
        run.status = RunStatus.PAUSED_CHECKPOINT.value
        await session.commit()
        return True

    async def execute_compiled_action(
        self,
        session,
        run: SolveRun,
        challenge: Challenge,
        attempt: RunAttempt,
        task: AgentTask,
        approved_action: ApprovedAction,
    ) -> AgentTaskResultContract:
        """Dispatch a compiled production action without another model turn.

        Planner, Analysis and the compiler have already supplied the decision
        and the schema-ready arguments.  Production execution therefore uses
        the immutable compiled payload directly; a production role must not
        emit a second RoleAction for the same ApprovedAction.
        """
        phase_before_dispatch = str(run.current_phase or "")

        async def checkpoint(code: str, reason: str) -> AgentTaskResultContract:
            approved_action.status = "REJECTED"
            if phase_before_dispatch:
                run.current_phase = phase_before_dispatch
            run.last_error_code = code
            run.last_error_message = reason[:4000]
            run.recovery_checkpoint_json = {
                "classification": code,
                "task_id": task.id,
                "approved_action_id": approved_action.id,
            }
            run.status = RunStatus.PAUSED_CHECKPOINT.value
            result = AgentTaskResultContract(
                task_id=task.id,
                status=AgentTaskStatus.FAILED,
                failure_classification={
                    "fingerprint": code.lower(),
                    "classification": code,
                    "retryable": True,
                    "reason": reason,
                    "next_allowed_condition": "create a fresh compiled action and task",
                },
                handoff_summary=reason[:4000],
            )
            await deterministic_controller.complete_task(session, task.id, result, task.lease_token)
            return result

        if approved_action.compile_status != "COMPILED" or not approved_action.compiled_arguments_json:
            return await checkpoint("COMPILED_ACTION_NOT_DISPATCHED", "The approved compiled action was not dispatchable.")
        if approved_action.status != "ACTIVE":
            return await checkpoint("COMPILED_ACTION_NOT_DISPATCHED", "The approved compiled action is not ACTIVE.")
        if approved_action.agent_role != task.agent_role or approved_action.tool_name not in (task.allowed_tools_json or []):
            return await checkpoint("COMPILED_ACTION_NOT_DISPATCHED", "The compiled action is outside the production task scope.")
        if approved_action.compiled_arguments_digest != (task.context_json or {}).get("compiled_arguments_digest"):
            return await checkpoint("COMPILED_ACTION_NOT_DISPATCHED", "The task context does not match the compiled action digest.")

        try:
            idle_expiry = task.idle_deadline_at
            if idle_expiry and idle_expiry.tzinfo is None:
                idle_expiry = idle_expiry.replace(tzinfo=UTC)
            remaining_idle = (idle_expiry - datetime.now(UTC)).total_seconds() if idle_expiry else None
            if remaining_idle is not None and remaining_idle <= 0:
                return await checkpoint("COMPILED_ACTION_NOT_DISPATCHED", "The production task idle deadline expired before dispatch.")
            timeout_seconds = float((task.budget_json or {}).get("max_runtime_seconds") or 300)
            if remaining_idle is not None:
                timeout_seconds = min(timeout_seconds, max(1.0, remaining_idle))
            logger.warning(
                "multi_agent.compiled_dispatch.begin run_id=%s task_id=%s approved_action_id=%s tool=%s timeout=%s",
                run.id, task.id, approved_action.id, approved_action.tool_name, timeout_seconds,
            )
            # Use one awaited coroutine for the gateway boundary.  The prior
            # detached task could retain the request AsyncSession while the
            # controller was waiting, leaving the production task RUNNING
            # without a durable ToolCall.
            # Keep gateway persistence out of the controller's orchestration
            # session.  The gateway writes ToolCall/Observation/Artifact rows
            # and performs Runner I/O; a separate MySQL session prevents a
            # shared-session wait from stranding the production task.
            used_separate_dispatch_session = False
            async with SessionLocal() as dispatch_session:
                dispatch_run = await dispatch_session.get(SolveRun, run.id)
                dispatch_challenge = await dispatch_session.get(Challenge, challenge.id)
                if dispatch_run is None or dispatch_challenge is None:
                    # Unit fixtures may intentionally keep the Run in an
                    # uncommitted outer transaction.  Preserve that test
                    # seam; production Runs are committed before dispatch.
                    result = await asyncio.wait_for(
                        self.tool_invoker(
                            session, run, challenge, approved_action.tool_name,
                            dict(approved_action.compiled_arguments_json),
                            execution_layer="multi_agent",
                            logical_tool_call_id=f"mcp:{run.id}:{task.id}:{uuid.uuid4().hex[:12]}",
                            agent_task_id=task.id, agent_role=task.agent_role,
                            task_lease_token=task.lease_token, approved_action_id=approved_action.id,
                        ), timeout=timeout_seconds,
                    )
                else:
                    used_separate_dispatch_session = True
                    logical_id = f"mcp:{run.id}:{task.id}:{uuid.uuid4().hex[:12]}"
                    gateway_task = asyncio.create_task(self.tool_invoker(
                        dispatch_session, dispatch_run, dispatch_challenge,
                        approved_action.tool_name, dict(approved_action.compiled_arguments_json),
                        execution_layer="multi_agent",
                        logical_tool_call_id=logical_id,
                        agent_task_id=task.id, agent_role=task.agent_role,
                        task_lease_token=task.lease_token, approved_action_id=approved_action.id,
                    ))
                    if approved_action.tool_name == "sql_boolean_compare":
                        # Five seconds is the dispatch deadline, not the
                        # bounded Boolean experiment's total runtime.  The
                        # gateway commits the ToolCall before Runner I/O.
                        done, _ = await asyncio.wait({gateway_task}, timeout=5.0)
                        if not done:
                            async with SessionLocal() as probe_session:
                                durable_call_id = await probe_session.scalar(select(ToolCall.id).where(
                                    ToolCall.run_id == run.id,
                                    ToolCall.agent_task_id == task.id,
                                    ToolCall.approved_action_id == approved_action.id,
                                ).order_by(ToolCall.created_at.desc()))
                            if not durable_call_id:
                                gateway_task.cancel()
                                with contextlib.suppress(asyncio.CancelledError):
                                    await gateway_task
                                raise asyncio.TimeoutError
                        result = await asyncio.wait_for(gateway_task, timeout=timeout_seconds)
                    else:
                        result = await asyncio.wait_for(gateway_task, timeout=timeout_seconds)
            # End the outer MySQL transaction before inspecting rows committed
            # by dispatch_session; otherwise MySQL Repeatable Read can keep
            # the Controller on a pre-dispatch snapshot and hide ToolCall.
            if used_separate_dispatch_session:
                await session.rollback()
                await session.refresh(run)
                await session.refresh(task)
                await session.refresh(approved_action)
                # The dispatch session is intentionally independent, but the
                # controller phase was committed before the gateway call. A
                # stale outer identity map must never regress it to INTAKE.
                if phase_before_dispatch and run.current_phase != phase_before_dispatch:
                    run.current_phase = phase_before_dispatch
                    await session.commit()
            logger.warning(
                "multi_agent.compiled_dispatch.returned run_id=%s task_id=%s tool=%s",
                run.id, task.id, approved_action.tool_name,
            )
        except asyncio.TimeoutError:
            return await checkpoint("COMPILED_ACTION_NOT_DISPATCHED", "The compiled action produced no durable ToolCall before the idle deadline.")
        except DomainError as error:
            schema_error = error.code in {
                "TOOL_INVALID_ARGUMENT",
                "COMPILED_ACTION_SCHEMA_INVALID",
                "APPROVED_ARGUMENT_DIGEST_MISMATCH",
                "TOOL_SCHEMA_VERSION_CHANGED",
            }
            code = "COMPILED_ACTION_SCHEMA_INVALID" if schema_error else error.code
            return await checkpoint(code, error.message)
        except Exception as error:
            return await checkpoint("COMPILED_ACTION_NOT_DISPATCHED", str(error))

        calls = list((await session.scalars(
            select(ToolCall).where(ToolCall.run_id == run.id, ToolCall.agent_task_id == task.id)
        )).all())
        if not calls:
            return await checkpoint("COMPILED_ACTION_NOT_DISPATCHED", "The compiled action completed without creating a ToolCall.")
        if str(result.get("status") or "").upper() != "COMPLETED":
            failure_code = str(result.get("error_code") or "TOOL_FAILURE")
            partial = AgentTaskResultContract(
                task_id=task.id,
                status=AgentTaskStatus.PARTIAL,
                failure_classification={
                    "fingerprint": "compiled-action-tool-failed",
                    "classification": failure_code,
                    "retryable": True,
                    "reason": str(result.get("error") or result.get("summary") or "The compiled tool action failed."),
                    "next_allowed_condition": "replan from the durable tool result",
                },
                handoff_summary="The compiled action was dispatched but the tool did not complete.",
            )
            await deterministic_controller.complete_task(session, task.id, partial, task.lease_token)
            return partial
        if not any(call.status == "COMPLETED" for call in calls):
            return await checkpoint("COMPILED_ACTION_NOT_DISPATCHED", "The compiled action did not produce a completed ToolCall.")
        # The gateway normally consumes the capability itself.  Keep the
        # lifecycle invariant at the controller boundary as well so test
        # invokers and alternate gateways cannot leave a completed action
        # ACTIVE.
        approved_action.status = "CONSUMED"
        completed = AgentTaskResultContract(
            task_id=task.id,
            status=AgentTaskStatus.COMPLETED,
            handoff_summary="The approved compiled action completed and produced durable evidence.",
        )
        return await self._complete(session, run, task, task.lease_token, completed)

    async def _candidate_gate(self, session, run: SolveRun) -> FlagCandidate | None:
        candidates = list((await session.scalars(select(FlagCandidate).where(FlagCandidate.run_id == run.id, FlagCandidate.verified.is_(False)).order_by(FlagCandidate.created_at.desc()))).all())
        for candidate in candidates:
            if not candidate.candidate or not candidate.source_artifact_id or not candidate.source_tool_call_id:
                continue
            artifact = await session.get(Artifact, candidate.source_artifact_id)
            call = await session.get(ToolCall, candidate.source_tool_call_id)
            if artifact and call and artifact.tool_call_id == call.id:
                return candidate
        return None

    async def _fresh_reproduction(self, run: SolveRun, candidate: FlagCandidate, artifact: Artifact | None) -> bool:
        if not artifact:
            return False
        path = Path(run.workspace_path, artifact.file_path)
        return path.is_file() and candidate.candidate in path.read_text(encoding="utf-8", errors="replace")

    async def _record_verified_fact_capabilities(self, session, run: SolveRun, fact: VerifiedFact) -> None:
        evidence = {"fact_id": fact.id, "fact_key": fact.fact_key, "evidence_ids": fact.evidence_ids_json, "value": fact.value_json}
        if fact.fact_key == "asset_warranty.valid_baseline":
            for capability in (
                "warranty_endpoint_identified",
                "request_contract_confirmed",
                "valid_business_baseline_confirmed",
            ):
                await solver_state_service.record_capability(session, run.id, capability, evidence=evidence)
        if fact.fact_key == "asset_warranty.invalid_baseline":
            await solver_state_service.record_capability(session, run.id, "invalid_business_baseline_confirmed", evidence=evidence)
        if fact.fact_key in {"asset_warranty.valid_baseline", "asset_warranty.invalid_baseline"}:
            verified_keys = set(
                (
                    await session.scalars(
                        select(VerifiedFact.fact_key).where(
                            VerifiedFact.run_id == run.id,
                            VerifiedFact.promotion_status == "VERIFIED",
                            VerifiedFact.fact_key.in_(["asset_warranty.valid_baseline", "asset_warranty.invalid_baseline"]),
                        )
                    )
                ).all()
            )
            if {"asset_warranty.valid_baseline", "asset_warranty.invalid_baseline"} <= verified_keys:
                await solver_state_service.record_capability(
                    session,
                    run.id,
                    "business_response_differential_confirmed",
                    evidence={"fact_keys": sorted(verified_keys), "source": "controller_fact_promotion"},
                )
        if fact.fact_type == "BOOLEAN_ORACLE" and isinstance(fact.value_json, dict):
            value = fact.value_json
            stability = value.get("repeat_stability") if isinstance(value.get("repeat_stability"), dict) else {}
            await solver_state_service.confirm_boolean_oracle(
                session,
                run,
                request_spec=dict(value.get("request_contract") or {}),
                test_field=str(value.get("test_field") or ""),
                baseline_value=str(value.get("baseline_value") or ""),
                control_fields=dict(value.get("control_fields") or {}),
                oracle=dict(value.get("oracle") or {}),
                evidence_ids=list(fact.evidence_ids_json or []),
                fact_ids=[fact.id],
                true_stable=stability.get("true") is not False,
                false_stable=stability.get("false") is not False,
                differential=value.get("response_differential") is not False,
            )
            await solver_state_service.record_capability(
                session,
                run.id,
                "mysql_boolean_oracle_confirmed" if fact.fact_key == "asset_warranty.mysql_boolean_oracle" else "boolean_oracle_confirmed",
                evidence=evidence,
            )
            await solver_state_service.record_capability(
                session,
                run.id,
                "boolean_predicate_oracle_confirmed",
                evidence=evidence,
            )
            if fact.fact_key == "asset_warranty.mysql_boolean_oracle":
                await solver_state_service.record_capability(
                    session,
                    run.id,
                    "sql_injection_confirmed",
                    evidence=evidence,
                )
        if fact.fact_key == "asset_warranty.oracle_calibration_matrix" and isinstance(fact.value_json, dict):
            calibration = fact.value_json
            profile = calibration.get("adaptive_extraction_profile") if isinstance(calibration.get("adaptive_extraction_profile"), dict) else {}
            capabilities = calibration.get("capabilities") if isinstance(calibration.get("capabilities"), dict) else {}
            state = await solver_state_service.load(session, run.id)
            if state is not None and profile:
                state.capability_ledger_json = {
                    **(state.capability_ledger_json or {}),
                    "adaptive_extraction_profile": {"confirmed": True, "value": profile, "evidence": evidence or {}},
                    "adaptive_extraction_profile_json": profile,
                }
                for name, confirmed in capabilities.items():
                    if confirmed:
                        state.capability_ledger_json[name] = {"confirmed": True, "evidence": evidence or {}, "profile_id": profile.get("profile_id")}
                    elif name in {"ascii_supported", "ord_supported", "hex_supported", "prefix_like_supported"}:
                        state.capability_ledger_json[name] = {"confirmed": False, "evidence": evidence or {}, "profile_id": profile.get("profile_id")}
                await session.commit()
            for capability, confirmed in capabilities.items():
                if confirmed and capability not in {"string_length_supported", "substring_supported", "direct_character_comparison_supported", "ascii_supported", "ord_supported", "hex_supported", "prefix_like_supported", "numeric_character_binary_search_supported", "bounded_character_enumeration_supported"}:
                    await solver_state_service.record_capability(session, run.id, capability, evidence={**(evidence or {}), "profile_id": profile.get("profile_id")})
            if profile:
                for capability in ("character_extraction_oracle_confirmed", "scalar_function_oracle_confirmed"):
                    await solver_state_service.record_capability(session, run.id, capability, evidence={**(evidence or {}), "profile_id": profile.get("profile_id")})
        metadata_capabilities = {
            "asset_warranty.mysql_version": "mysql_dbms_confirmed",
            "asset_warranty.mysql_version_comment": "mysql_dbms_confirmed",
            "asset_warranty.current_database": "current_database_identified",
            "asset_warranty.mysql_user_tables": "mysql_user_tables_discovered",
            "asset_warranty.mysql_candidate_columns": "mysql_candidate_columns_discovered",
        }
        if fact.fact_key in metadata_capabilities:
            await solver_state_service.record_capability(session, run.id, metadata_capabilities[fact.fact_key], evidence=evidence)
            keys = set((await session.scalars(select(VerifiedFact.fact_key).where(
                VerifiedFact.run_id == run.id,
                VerifiedFact.promotion_status == "VERIFIED",
            ))).all())
            required = {
                "asset_warranty.mysql_version",
                "asset_warranty.mysql_version_comment",
                "asset_warranty.current_database",
                "asset_warranty.mysql_user_tables",
                "asset_warranty.mysql_candidate_columns",
            }
            if required <= keys:
                await solver_state_service.record_capability(
                    session,
                    run.id,
                    "mysql_metadata_discovered",
                    evidence={"fact_keys": sorted(required), "source": "controller_metadata_promotion"},
                )

    async def run(self, session, run: SolveRun, challenge: Challenge, attempt: RunAttempt, lease: RunExecutionLease, *, engine: object | None = None) -> dict:
        await deterministic_controller.seed_policies(session)
        if not await self._ensure_asset_warranty_metadata_or_pause(session, run, challenge):
            return {"status": run.status, "error_code": run.last_error_code, "current_phase": run.current_phase}
        await solver_state_service.initialize(session, run, challenge.challenge_type, [], challenge.name, challenge.description)
        self.runtime.engine = engine or self.runtime.engine
        self.runtime.tool_invoker = self.tool_invoker
        try:
            if await self._recover_mysql_boolean_oracle(session, run):
                return {"status": run.status, "current_phase": "CHAINING", "boolean_oracle_confirmed": True, "recovered": True}
            if RunStatus(run.status) in {RunStatus.CREATED, RunStatus.RUNNING}:
                await self._status(session, run, RunStatus.PREPARING)
                await self._status(session, run, RunStatus.ANALYZING)
                await self._status(session, run, RunStatus.PLANNING)
            elif RunStatus(run.status) in {RunStatus.WAITING_USER, RunStatus.PAUSED_DEPLOYMENT, RunStatus.PAUSED_RECOVERY, RunStatus.PAUSED_CHECKPOINT, RunStatus.PAUSED_RATE_LIMIT}:
                await self._status(session, run, RunStatus.PLANNING)
            max_cycles = self._max_replan_cycles(run, challenge)
            parent: str | None = None
            context: dict = {}
            for cycle in range(max_cycles):
                await self._phase(session, run, await self._capability_phase(session, run))
                planner_task, planner_token = await self._task(session, run, AgentRole.PLANNER, AgentTaskKind.PLANNING, "Select the next bounded stage from the current memory snapshot.", [], parent=parent, context=context)
                planner_result = await self._complete(session, run, planner_task, planner_token, await self.runtime.execute(session, run, challenge, attempt, planner_task, planner_token))
                if planner_result.status == AgentTaskStatus.FAILED:
                    run.last_error_code = "MODEL_OUTPUT_SCHEMA_INVALID"
                    run.last_error_message = planner_result.handoff_summary
                    run.recovery_checkpoint_json = {"classification": "MODEL_OUTPUT_SCHEMA_INVALID", "task_id": planner_task.id}
                    await self._status(session, run, RunStatus.PAUSED_CHECKPOINT)
                    return {"status": run.status, "error_code": run.last_error_code, "agent_tasks": cycle + 1}
                proposal = await self._proposal(session, run, challenge, planner_task, planner_result)
                await self._memory(session, run, stage=proposal.current_stage, task=planner_task, working={"proposal": proposal.proposal_id, "decision_question": proposal.decision_question, "cycle": cycle})
                parent = planner_task.id

                # PLAN_REVIEW is a mandatory gate for every production role.
                review_context = {"proposal": {"proposal_id": proposal.proposal_id, "current_stage": proposal.current_stage, "decision_question": proposal.decision_question, "next_agent": proposal.next_agent, "objective": proposal.objective, "allowed_tools": proposal.allowed_tools_json, "success_condition": proposal.success_condition, "stop_conditions": proposal.stop_conditions_json, "budget": proposal.budget_json}, "plan_review": True}
                plan_task, plan_token = await self._task(session, run, AgentRole.ANALYSIS, AgentTaskKind.PLAN_REVIEW, "Audit this proposal before any production tool call.", [], parent=planner_task.id, context=review_context)
                # Keep the Analysis task RUNNING while its contract is
                # validated and its durable dispatch chain is written.  The
                # old order completed the task first, so a failed Review
                # insert stranded the Run in PLANNING forever.
                plan_result = await self.runtime.execute(session, run, challenge, attempt, plan_task, plan_token)
                try:
                    plan_review, approved, production_task, production_token = await asyncio.wait_for(
                        self._persist_plan_review(session, run, challenge, proposal, plan_task, plan_token, plan_result),
                        timeout=5.0,
                    )
                except asyncio.TimeoutError:
                    await self._fail_plan_review_persistence(
                        session,
                        run.id,
                        plan_task.id,
                        plan_token,
                        "PLAN_REVIEW persistence/dispatch exceeded the 5 second deadline.",
                    )
                    return {"status": RunStatus.PAUSED_CHECKPOINT.value, "error_code": "PLAN_REVIEW_PERSISTENCE_INCOMPLETE", "agent_tasks": cycle + 1}
                except DomainError as error:
                    if error.code == "MODEL_OUTPUT_SCHEMA_INVALID" and plan_result.status != AgentTaskStatus.COMPLETED:
                        run.last_error_code = error.code
                        run.last_error_message = error.message[:4000]
                        run.recovery_checkpoint_json = {"classification": error.code, "task_id": plan_task.id}
                        await self._status(session, run, RunStatus.PAUSED_CHECKPOINT)
                        return {"status": run.status, "error_code": error.code, "agent_tasks": cycle + 1}
                    if error.code == "APPROVED_ACTION_COMPILE_FAILED":
                        run.last_error_code = error.code
                        run.last_error_message = error.message[:4000]
                        run.recovery_checkpoint_json = {
                            "classification": error.code,
                            "details": error.details or {},
                            "proposal_id": proposal.id,
                            "analysis_task_id": plan_task.id,
                        }
                        await self._status(session, run, RunStatus.PAUSED_CHECKPOINT)
                        await session.commit()
                        return {"status": run.status, "error_code": error.code, "agent_tasks": cycle + 1}
                    if error.code != "PLAN_REVIEW_PERSISTENCE_INCOMPLETE":
                        run.last_error_code = error.code
                        run.last_error_message = error.message[:4000]
                        run.recovery_checkpoint_json = {
                            "classification": error.code,
                            "details": error.details or {},
                            "proposal_id": proposal.id,
                            "analysis_task_id": plan_task.id,
                        }
                        await self._status(session, run, RunStatus.PAUSED_CHECKPOINT)
                        await session.commit()
                        return {"status": run.status, "error_code": error.code, "agent_tasks": cycle + 1}
                    await self._fail_plan_review_persistence(session, run.id, plan_task.id, plan_token, str(error))
                    return {"status": RunStatus.PAUSED_CHECKPOINT.value, "error_code": "PLAN_REVIEW_PERSISTENCE_INCOMPLETE", "agent_tasks": cycle + 1}
                if plan_review.decision != AnalysisDecision.APPROVE.value:
                    context = {"replan_reason": f"PLAN_REVIEW_{plan_review.decision}"}
                    await self._memory(session, run, stage="HYPOTHESIS", task=plan_task, working=context | {"plan_review": plan_review.decision})
                    await session.commit()
                    await self._status(session, run, RunStatus.PLANNING)
                    continue
                role = AgentRole(proposal.next_agent)
                if role == AgentRole.VERIFY and not await self._candidate_gate(session, run):
                    context = {"replan_reason": "VERIFY_GATE_REQUIRES_CANDIDATE_ARTIFACT_TOOLCALL"}
                    await self._memory(session, run, stage="FLAG_SEARCH", task=plan_task, working=context)
                    await session.commit()
                    continue
                # _persist_plan_review already created and leased the
                # production task before completing AnalysisTask.
                exec_task, exec_token = production_task, production_token
                if exec_task is None or exec_token is None:
                    raise DomainError("PLAN_REVIEW_PERSISTENCE_INCOMPLETE", "Approved PLAN_REVIEW has no production task lease.")
                await self._phase(session, run, "FLAG_VERIFICATION" if role == AgentRole.VERIFY else proposal.current_stage)
                await self._status(session, run, RunStatus.EXECUTING)
                if approved is None:
                    raise DomainError("PLAN_REVIEW_PERSISTENCE_INCOMPLETE", "Approved PLAN_REVIEW has no ApprovedAction.")
                if approved.tool_name == "sql_boolean_compare":
                    await self._controller_event(
                        session,
                        run.id,
                        "boolean.action.dispatched",
                        {
                            "run_id": run.id,
                            "attempt_id": attempt.id,
                            "task_id": exec_task.id,
                            "proposal_id": proposal.id,
                            "approved_action_id": approved.id,
                            "timestamp": datetime.now(UTC).isoformat(),
                        },
                    )
                # Production roles execute the Controller-compiled capability
                # directly.  There is no second model/RoleAction decision for
                # an action already approved by Analysis and compiled by the
                # controller.
                exec_result = await self.execute_compiled_action(
                    session, run, challenge, attempt, exec_task, approved
                )
                # execute_compiled_action records the production task result
                # after the gateway's independent dispatch session returns.
                # Commit that handoff before building Result Context so the
                # next controller stage does not retain an uncommitted task
                # row/lease transaction.
                await session.commit()
                if exec_result.status in {AgentTaskStatus.FAILED, AgentTaskStatus.PARTIAL}:
                    failure_classification = normalize_failure_classification(exec_result.failure_classification)
                    failure_classification = str(failure_classification.get("classification") or "")
                    metadata_empty = failure_classification in {"MYSQL_METADATA_EMPTY_RESULT", "ORACLE_RESPONSE_UNRECOGNIZED"}
                    failure_entry = await record_tool_failure(session, run, approved, failure_classification or "TOOL_FAILURE")
                    if failure_entry["count"] >= 2 and not metadata_empty:
                        run.last_error_code = failure_classification or "TOOL_FAILURE_REPEATED"
                        run.last_error_message = "The same tool and arguments failed twice; further identical execution is blocked."
                        run.recovery_checkpoint_json = {
                            **(run.recovery_checkpoint_json or {}),
                            "current_phase": run.current_phase,
                            "repeated_failure": failure_entry,
                            "question": "The same tool and arguments failed twice. Fix the tool/target or choose another strategy.",
                            "options": ["retry_after_fix", "finish_unsolved_wp", "try_alternative_strategy"],
                        }
                        await self._status(session, run, RunStatus.WAITING_USER)
                        await session.commit()
                        return {"status": run.status, "error_code": run.last_error_code, "repeated_failure": True}
                    if approved.tool_name == "mysql_metadata_discovery" and metadata_empty:
                        paused = await self._handle_mysql_metadata_empty_result(session, run, challenge, approved, exec_task)
                        if not paused:
                            parent = exec_task.id
                            context = {"replan_reason": "MYSQL_METADATA_EMPTY_RESULT", "metadata_stage": (approved.compiled_arguments_json or {}).get("stage")}
                            continue
                    await session.commit()
                    return {
                        "status": run.status,
                        "error_code": run.last_error_code,
                        "agent_tasks": int(await session.scalar(select(func.count()).select_from(AgentTask).where(AgentTask.run_id == run.id)) or 0),
                    }
                await self._status(session, run, RunStatus.EVALUATING)
                await self._controller_event(
                    session,
                    run.id,
                    "production.result_context.started",
                    {
                        "run_id": run.id,
                        "attempt_id": attempt.id,
                        "task_id": exec_task.id,
                        "proposal_id": proposal.id,
                        "approved_action_id": approved.id,
                        "timestamp": datetime.now(UTC).isoformat(),
                    },
                )
                try:
                    result_payload = await asyncio.wait_for(
                        self._result_context(session, run, attempt, proposal, plan_review, approved, exec_task),
                        timeout=10.0,
                    )
                except asyncio.TimeoutError as error:
                    raise DomainError(
                        "RESULT_CONTEXT_TIMEOUT",
                        "Production Result Context construction exceeded its bounded deadline.",
                        {"task_id": exec_task.id, "approved_action_id": approved.id},
                    ) from error
                await self._controller_event(
                    session,
                    run.id,
                    "production.result_context.completed",
                    {
                        "run_id": run.id,
                        "attempt_id": attempt.id,
                        "task_id": exec_task.id,
                        "proposal_id": proposal.id,
                        "approved_action_id": approved.id,
                        "tool_call_count": len(result_payload.get("tool_calls") or []),
                        "artifact_count": len(result_payload.get("artifacts") or []),
                        "evidence_count": len(result_payload.get("evidence_ids") or []),
                        "timestamp": datetime.now(UTC).isoformat(),
                    },
                )
                result_review_task, result_review_token = await self._task(session, run, AgentRole.ANALYSIS, AgentTaskKind.RESULT_REVIEW, "Review the complete producing task result, ToolCalls, Artifacts and Evidence.", [], parent=exec_task.id, context=result_payload)
                await self._controller_event(
                    session,
                    run.id,
                    "analysis.result_review.dispatched",
                    {
                        "run_id": run.id,
                        "attempt_id": attempt.id,
                        "task_id": result_review_task.id,
                        "producing_task_id": exec_task.id,
                        "proposal_id": proposal.id,
                        "approved_action_id": approved.id,
                        "timestamp": datetime.now(UTC).isoformat(),
                    },
                )
                result_review = await self._complete(session, run, result_review_task, result_review_token, await self.runtime.execute(session, run, challenge, attempt, result_review_task, result_review_token))
                result_review_row = await self._review(session, run, proposal, result_review_task, result_review)
                await self._controller_event(
                    session,
                    run.id,
                    "analysis.result_review.completed",
                    {
                        "run_id": run.id,
                        "attempt_id": attempt.id,
                        "task_id": result_review_task.id,
                        "proposal_id": proposal.id,
                        "analysis_review_id": result_review_row.id,
                        "decision": result_review_row.decision,
                        "timestamp": datetime.now(UTC).isoformat(),
                    },
                )
                try:
                    promoted = await self._apply_result_review(session, run, exec_task, result_review_row)
                except DomainError as error:
                    if error.code != "RESULT_REVIEW_PROMOTION_EMPTY":
                        raise
                    repair_task, repair_token = await self._task(session, run, AgentRole.ANALYSIS, AgentTaskKind.RESULT_REVIEW, "Repair the approved result review by selecting candidate facts or explicitly accepting a capability.", [], parent=result_review_task.id, context={**result_payload, "repair_attempt": True})
                    repair_result = await self._complete(session, run, repair_task, repair_token, await self.runtime.execute(session, run, challenge, attempt, repair_task, repair_token))
                    result_review_row = await self._review(session, run, proposal, repair_task, repair_result)
                    promoted = await self._apply_result_review(session, run, exec_task, result_review_row)
                await self._controller_event(
                    session,
                    run.id,
                    "promotion.completed",
                    {
                        "run_id": run.id,
                        "attempt_id": attempt.id,
                        "task_id": exec_task.id,
                        "proposal_id": proposal.id,
                        "analysis_review_id": result_review_row.id,
                        "promoted_fact_ids": promoted,
                        "capabilities_added": result_review_row.capabilities_added_json or [],
                        "timestamp": datetime.now(UTC).isoformat(),
                    },
                )
                if str(proposal.current_stage or "").upper() == "MYSQL_METADATA_DISCOVERY" and promoted:
                    metadata_call = await session.scalar(select(ToolCall).where(
                        ToolCall.run_id == run.id,
                        ToolCall.agent_task_id == exec_task.id,
                        ToolCall.status == "COMPLETED",
                    ).order_by(ToolCall.created_at.desc()))
                    node_id = f"mysql-metadata-{exec_task.id}"
                    existing_node = await session.scalar(select(SolutionChainNode).where(
                        SolutionChainNode.run_id == run.id,
                        SolutionChainNode.node_id == node_id,
                    ))
                    if existing_node is None:
                        promoted_facts = list((await session.scalars(select(VerifiedFact).where(VerifiedFact.id.in_(promoted)))).all())
                        capabilities = {"MYSQL_VERSION": "mysql_dbms_confirmed", "MYSQL_VERSION_COMMENT": "mysql_dbms_confirmed", "CURRENT_DATABASE": "current_database_identified", "MYSQL_USER_TABLES": "mysql_user_tables_discovered", "MYSQL_CANDIDATE_COLUMNS": "mysql_candidate_columns_discovered"}
                        capability = next((capabilities.get(fact.fact_type) for fact in promoted_facts if capabilities.get(fact.fact_type)), "mysql_metadata_discovered")
                        session.add(SolutionChainNode(
                            run_id=run.id,
                            node_id=node_id,
                            stage="MYSQL_METADATA_DISCOVERY",
                            objective=proposal.objective,
                            input_fact_ids_json=proposal.input_fact_ids_json or [],
                            agent_task_id=exec_task.id,
                            logical_tool_call_id=metadata_call.logical_tool_call_id if metadata_call else None,
                            result_fact_ids_json=promoted,
                            capability_added=capability,
                            evidence_ids_json=exec_result.evidence_ids,
                            status="ACCEPTED",
                        ))
                        await session.flush()
                next_phase = result_review_row.next_phase if result_review_row.decision == AnalysisDecision.APPROVE.value else "HYPOTHESIS"
                run.current_phase = next_phase
                state_after_review = await solver_state_service.load(session, run.id)
                if state_after_review is not None:
                    state_after_review.current_phase = next_phase
                await self._memory(session, run, stage=next_phase, task=result_review_task, working={"last_role": role.value, "last_review": result_review_row.decision, "promoted_fact_ids": promoted, "last_evidence_ids": exec_result.evidence_ids, "candidate_seen": bool(await self._candidate_gate(session, run))})
                if str(proposal.current_stage or "").upper() == "ORACLE_CALIBRATION":
                    calibration_fact = next(iter((await session.scalars(select(VerifiedFact).where(
                        VerifiedFact.id.in_(promoted),
                        VerifiedFact.fact_key == "asset_warranty.oracle_calibration_matrix",
                    ))).all()), None)
                    calibration_value = calibration_fact.value_json if calibration_fact and isinstance(calibration_fact.value_json, dict) else {}
                    has_profile = isinstance(calibration_value.get("adaptive_extraction_profile"), dict) and bool(calibration_value.get("adaptive_extraction_profile", {}).get("extraction_strategy"))
                    if not has_profile:
                        run.current_phase = "TESTING"
                        if state_after_review is not None:
                            state_after_review.current_phase = "TESTING"
                        run.last_error_code = str(calibration_value.get("error_code") or "MYSQL_PREDICATE_NOT_CONFIRMED")
                        run.last_error_message = "Expression Oracle calibration did not establish the required predicate semantics."[:4000]
                        run.recovery_checkpoint_json = {
                            "checkpoint_type": "ORACLE_CALIBRATION_FAILED",
                            "current_phase": "TESTING",
                            "last_passed_level": max((int(key) for key, value in (calibration_value.get("levels") or {}).items() if value), default=-1),
                            "first_failed_level": next((int(item.get("level")) for item in (calibration_value.get("calibration_matrix") or []) if item.get("passed") is False), None),
                            "predicate_template": calibration_value.get("predicate_template"),
                            "verified_fact_ids": promoted,
                        }
                        await self._status(session, run, RunStatus.PAUSED_CHECKPOINT)
                        await session.commit()
                        return {"status": run.status, "current_phase": "TESTING", "error_code": run.last_error_code, "calibration_failed": True}
                if str(proposal.current_stage or "").upper() == "BOOLEAN_ORACLE" and not promoted:
                    challenge_metadata = (challenge.metadata_json or {}) if challenge else {}
                    declared_fields = {str(item) for item in (challenge_metadata.get("fields") or []) if str(item)}
                    completed_calls = list((await session.scalars(select(ToolCall).where(
                        ToolCall.run_id == run.id,
                        ToolCall.tool_name == "sql_boolean_compare",
                        ToolCall.status == "COMPLETED",
                    ))).all())
                    tested_fields: set[str] = set()
                    for completed_call in completed_calls:
                        arguments = completed_call.arguments_json
                        if isinstance(arguments, str):
                            with contextlib.suppress(Exception):
                                arguments = json.loads(arguments)
                        if isinstance(arguments, dict) and arguments.get("test_field"):
                            tested_fields.add(str(arguments["test_field"]))
                    if self._asset_warranty_mysql(challenge) and declared_fields and declared_fields <= tested_fields:
                        run.last_error_code = "MYSQL_PREDICATE_NOT_CONFIRMED"
                        run.last_error_message = "All declared business fields produced no stable TRUE/FALSE Boolean Oracle differential."
                        run.recovery_checkpoint_json = {
                            "checkpoint_type": "BOOLEAN_ORACLE_CALIBRATION_FAILED",
                            "classification": "MYSQL_PREDICATE_NOT_CONFIRMED",
                            "current_phase": "TESTING",
                            "tested_fields": sorted(tested_fields),
                            "declared_fields": sorted(declared_fields),
                            "tool_call_ids": [item.id for item in completed_calls],
                        }
                        await run_finalizer.finish_unsolved_with_wp(
                            session, run, "MYSQL_PREDICATE_NOT_CONFIRMED: all declared business fields were tested without a stable Boolean Oracle."
                        )
                        return {"status": run.status, "current_phase": "REPORTING", "error_code": run.last_error_code, "boolean_oracle_failed": True, "wp": True}
                # A confirmed Boolean Oracle is the handoff into Block 3. Keep
                # the durable checkpoint for recovery, but continue the same
                # fresh Run into calibration and metadata discovery instead of
                # requiring a manual restart between checkpoints.
                boolean_oracle_promoted = False
                for fact_id in promoted:
                    fact = await session.get(VerifiedFact, fact_id)
                    if fact is not None and fact.fact_key == "asset_warranty.mysql_boolean_oracle":
                        boolean_oracle_promoted = True
                        break
                if boolean_oracle_promoted:
                    run.current_phase = "CHAINING"
                    if state_after_review is not None:
                        state_after_review.current_phase = "CHAINING"
                    run.recovery_checkpoint_json = {
                        "checkpoint_type": "MYSQL_BOOLEAN_ORACLE_CONFIRMED",
                        "current_phase": "CHAINING",
                        "do_not_repeat": ["valid_baseline", "invalid_baseline", "sql_boolean_compare"],
                        "next_required_action": "BLOCK_3_METADATA_DISCOVERY",
                        "verified_fact_ids": promoted,
                    }
                    await self._status(session, run, RunStatus.PLANNING)
                    await session.flush()
                state_after_metadata = await solver_state_service.load(session, run.id)
                if state_after_metadata is not None and "mysql_metadata_discovered" in (state_after_metadata.capability_ledger_json or {}):
                    run.current_phase = "ENUMERATION"
                    state_after_metadata.current_phase = "ENUMERATION"
                    run.recovery_checkpoint_json = {
                        "checkpoint_type": "MYSQL_METADATA_DISCOVERED",
                        "current_phase": "ENUMERATION",
                        "do_not_repeat": ["valid_baseline", "invalid_baseline", "sql_boolean_compare", "mysql_metadata_discovery"],
                        "next_required_action": "BLOCK_4_BOUNDED_EXTRACTION",
                        "verified_fact_ids": promoted,
                    }
                    await self._status(session, run, RunStatus.PLANNING)
                    await session.flush()
                if role == AgentRole.VERIFY:
                    candidate = await self._candidate_gate(session, run)
                    call = await session.scalar(select(ToolCall).where(ToolCall.agent_task_id == exec_task.id).order_by(ToolCall.created_at.desc()))
                    artifact = await session.scalar(select(Artifact).where(Artifact.tool_call_id == call.id).order_by(Artifact.created_at.desc())) if call else None
                    reproduced = bool(candidate and artifact and await self._fresh_reproduction(run, candidate, artifact))
                    if candidate and call and artifact and reproduced and exec_result.evidence_ids and result_review_row.decision == AnalysisDecision.APPROVE.value:
                        await self._status(session, run, RunStatus.VERIFYING_FLAG)
                        await deterministic_controller.finalize_verified_candidate(session, run, candidate=candidate.candidate, verify_task_id=exec_task.id, source_artifact_id=artifact.id, producing_tool_call_id=call.id, evidence_ids=exec_result.evidence_ids, pattern_matched=True, fresh_reproduction=True, assistance_level=run.assistance_level or "AUTONOMOUS")
                        await session.commit()
                        return {"status": run.status, "agent_tasks": int(await session.scalar(select(func.count()).select_from(AgentTask).where(AgentTask.run_id == run.id)) or 0), "fresh_reproduction": True}
                context = {"replan_reason": "RESULT_REVIEW_CONTINUE", "approved_review": {"proposal_id": proposal.proposal_id, "allowed_tools": proposal.allowed_tools_json, "compiled_arguments_digest": approved.compiled_arguments_digest if approved else None}}
                await self._controller_event(
                    session,
                    run.id,
                    "planner.replan.dispatched",
                    {
                        "run_id": run.id,
                        "attempt_id": attempt.id,
                        "parent_task_id": result_review_task.id,
                        "proposal_id": proposal.id,
                        "timestamp": datetime.now(UTC).isoformat(),
                    },
                )
                await self._status(session, run, RunStatus.PLANNING)
            if not await self._asset_warranty_mysql_finish_ready(session, run, challenge):
                verified_keys = set((await session.scalars(select(VerifiedFact.fact_key).where(
                    VerifiedFact.run_id == run.id,
                    VerifiedFact.promotion_status == "VERIFIED",
                ))).all())
                metadata_required = {
                    "asset_warranty.mysql_version",
                    "asset_warranty.mysql_version_comment",
                    "asset_warranty.current_database",
                    "asset_warranty.mysql_user_tables",
                    "asset_warranty.mysql_candidate_columns",
                }
                metadata_complete = metadata_required <= verified_keys
                terminal_error = "BOUNDED_EXTRACTION_REQUIRED" if metadata_complete else "MYSQL_METADATA_DISCOVERY_REQUIRED"
                run.recovery_checkpoint_json = {
                    "terminal_reason": "FINISH_GATE_BLOCKED_INCOMPLETE_SOLUTION_CHAIN",
                    "cycles": max_cycles,
                    "required": [
                        "baseline_verified",
                        "database_type_verified",
                        "version_verified",
                        "current_database_verified",
                        "mysql_metadata_discovered",
                    ],
                }
                run.last_error_code = terminal_error
                run.last_error_message = (
                    "Finish gate blocked: MySQL metadata discovery is incomplete."
                    if not metadata_complete else
                    "Finish gate blocked: metadata is complete but bounded extraction/flag verification is still required."
                )
                await self._phase(session, run, "ENUMERATION")
                await self._status(session, run, RunStatus.PAUSED_CHECKPOINT)
                await session.commit()
                return {"status": run.status, "agent_tasks": int(await session.scalar(select(func.count()).select_from(AgentTask).where(AgentTask.run_id == run.id)) or 0), "terminal_reason": "FINISH_GATE_BLOCKED_INCOMPLETE_SOLUTION_CHAIN"}
            await run_finalizer.finish_unsolved_with_wp(
                session, run, "No candidate satisfied the verification gate after bounded replanning cycles."
            )
            return {"status": run.status, "agent_tasks": int(await session.scalar(select(func.count()).select_from(AgentTask).where(AgentTask.run_id == run.id)) or 0), "terminal_reason": "MAX_REPLAN_CYCLES_EXHAUSTED"}
        except DomainError as error:
            run.last_error_code = error.code
            run.last_error_message = error.message[:4000]
            run.recovery_checkpoint_json = {"terminal_reason": error.code, "details": error.details or {}}
            control_errors = {"TOOL_INVALID_ARGUMENT", "APPROVED_ACTION_COMPILE_FAILED", "APPROVED_ACTION_NOT_COMPILED", "TOOL_SCHEMA_VERSION_CHANGED", "SQL_EXPRESSION_PROVENANCE_REQUIRED", "RESULT_REVIEW_PROMOTION_EMPTY", "APPROVED_ARGUMENT_DIGEST_MISMATCH", "EXPERIMENT_ALREADY_CONFIRMED", "RESULT_CONTEXT_TIMEOUT", "RESULT_CONTEXT_DURABILITY_TIMEOUT", "RESULT_CONTEXT_RECORD_MISSING", "RESULT_CONTEXT_TOOLCALL_MISSING", "RESULT_CONTEXT_ARTIFACT_MISSING", "RESULT_CONTEXT_OBSERVATION_MISSING", "RESULT_CONTEXT_EVIDENCE_MISSING", "RESULT_CONTEXT_TASK_NOT_COMPLETED", "RESULT_CONTEXT_TASK_RESULT_MISSING", "RESULT_CONTEXT_APPROVED_ACTION_NOT_CONSUMED"}
            if error.code in control_errors:
                active_tasks = list((await session.scalars(select(AgentTask).where(AgentTask.run_id == run.id, AgentTask.status == AgentTaskStatus.RUNNING.value))).all())
                for active_task in active_tasks:
                    active_task.status = AgentTaskStatus.NEED_REPLAN.value
                actions = list((await session.scalars(select(ApprovedAction).where(ApprovedAction.run_id == run.id, ApprovedAction.status == "ACTIVE"))).all())
                for action in actions:
                    action.status = "REJECTED"
                target = RunStatus.PAUSED_RECOVERY if error.code in {"RESULT_CONTEXT_DURABILITY_TIMEOUT", "RESULT_CONTEXT_TASK_NOT_COMPLETED", "RESULT_CONTEXT_TASK_RESULT_MISSING", "RESULT_CONTEXT_APPROVED_ACTION_NOT_CONSUMED"} else RunStatus.PLANNING if error.code == "RESULT_REVIEW_PROMOTION_EMPTY" else RunStatus.PAUSED_CHECKPOINT
            else:
                target = RunStatus.PAUSED_CHECKPOINT if error.code == "MODEL_OUTPUT_SCHEMA_INVALID" else RunStatus.PAUSED_DEPLOYMENT if error.code in {"TARGET_NETWORK_ENFORCEMENT_UNAVAILABLE", "SCRIPT_TARGET_NETWORK_UNAVAILABLE", "TOOL_CATALOG_DRIFT"} else RunStatus.PAUSED_RECOVERY if error.code in {"RUNNER_UNAVAILABLE", "CODEX_STREAM_INTERRUPTED"} else RunStatus.FAILED_ENGINE
            if RunStatus(run.status) not in {RunStatus.COMPLETED_SOLVED, RunStatus.COMPLETED_UNSOLVED, RunStatus.CANCELLED}:
                await self._status(session, run, target)
            return {"status": run.status, "error_code": error.code, "agent_tasks": int(await session.scalar(select(func.count()).select_from(AgentTask).where(AgentTask.run_id == run.id)) or 0)}


multi_agent_orchestrator = MultiAgentOrchestrator()
