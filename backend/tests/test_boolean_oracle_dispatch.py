import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.challenge import Challenge
from app.models.multi_agent import AgentTask, AgentTaskResult, AnalysisReview, ApprovedAction, PlannerProposal, VerifiedFact
from app.models.run import Artifact, Observation, SolveRun, ToolCall
from app.models.solver_state import SolverState
from app.orchestration.multi_agent_orchestrator import MultiAgentOrchestrator
from app.schemas.multi_agent import AgentRole, AgentTaskContract, AgentTaskKind, TaskBudget
from app.services.approved_action_compiler import approved_action_compiler
from app.services.multi_agent import deterministic_controller
from app.tools.registry import load_tool_definitions


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _setup(session, tmp_path):
    challenge = Challenge(
        name="Warranty challenge",
        target_url="http://warranty.test:28036",
        allowed_hosts=["warranty.test"],
        metadata_json={
            "adapter": "asset_warranty",
            "dbms": "mysql",
            "endpoint": "/api/warranty/check",
            "method": "POST",
            "content_type": "application/json",
            "fields": ["asset_no", "department"],
            "control_values": {"asset_no": "PC-2026-013", "department": "OPS"},
        },
    )
    session.add(challenge)
    await session.flush()
    run = SolveRun(challenge_id=challenge.id, workspace_path=str(tmp_path), status="EXECUTING", current_phase="BASELINE")
    session.add(run)
    await session.flush()
    session.add(SolverState(run_id=run.id, current_phase="BASELINE", capability_ledger_json={}))
    proposal = PlannerProposal(
        run_id=run.id,
        proposal_id="PP-BOOLEAN-1",
        current_stage="EXPLOITATION",
        next_agent="EXPLOIT",
        objective="Confirm the bounded department boolean oracle",
        allowed_tools_json=["sql_boolean_compare"],
        budget_json={"max_logical_calls": 1, "max_internal_requests": 8, "max_runtime_seconds": 30},
        success_condition="boolean oracle confirmed",
    )
    session.add(proposal)
    await session.flush()
    review = AnalysisReview(
        proposal_id=proposal.id,
        task_kind="PLAN_REVIEW",
        decision="APPROVE",
        question_being_tested="Does department produce a stable boolean differential?",
        expected_true_signal_json={"matched": True},
        expected_false_signal_json={"matched": False},
        recommended_tool="sql_boolean_compare",
        approved_arguments_json={"test_field": "department"},
    )
    session.add(review)
    await session.flush()
    compiled = await approved_action_compiler.compile(session, run, challenge, proposal, review, "sql_boolean_compare")
    approved = ApprovedAction(
        run_id=run.id,
        approved_action_id="AA-BOOLEAN-1",
        proposal_id=proposal.id,
        analysis_review_id=review.id,
        agent_role="EXPLOIT",
        tool_name="sql_boolean_compare",
        compiled_arguments_json=compiled.arguments,
        compiled_arguments_digest=compiled.arguments_digest,
        tool_schema_hash=compiled.tool_schema_hash,
        compiler_name=compiled.compiler_name,
        compiler_version=compiled.compiler_version,
        compile_status="COMPILED",
        status="ACTIVE",
        max_logical_calls=1,
        expires_at=datetime.now(UTC),
    )
    session.add(approved)
    await session.flush()
    await deterministic_controller.seed_policies(session)
    task = await deterministic_controller.create_task(
        session,
        AgentTaskContract(
            task_id="AT-BOOLEAN-1",
            run_id=run.id,
            agent_role=AgentRole.EXPLOIT,
            task_kind=AgentTaskKind.EXPLOIT,
            objective="Execute the approved boolean oracle",
            allowed_tools=["sql_boolean_compare"],
            budget=TaskBudget(max_logical_calls=1, max_internal_requests=8, max_runtime_seconds=30),
            context={"approved_action_id": approved.id, "compiled_arguments_digest": compiled.arguments_digest},
        ),
    )
    token = await deterministic_controller.claim_task(session, task.id, "test-controller", lease_seconds=30)
    return challenge, run, proposal, approved, task, token


def _oracle_payload():
    signature_true = {"status_code": 200, "matched": True, "body_length": 17}
    signature_false = {"status_code": 200, "matched": False, "body_length": 18}
    return {
        "status": "COMPLETED",
        "summary": "Boolean SQL differential completed",
        "structured_result": {
            "status": "COMPLETED",
            "boolean_oracle_confirmed": True,
            "stable_true": True,
            "stable_false": True,
            "true_false_differential": True,
            "true_results": [{"signature": signature_true}],
            "false_results": [{"signature": signature_false}],
        },
    }


def _fake_invoker(tmp_path, *, result=None, error=None):
    calls = []

    async def invoke(session, run, challenge, name, arguments, **kwargs):
        calls.append((name, arguments, kwargs))
        if error:
            raise error
        payload = result or _oracle_payload()
        output = tmp_path / "oracle.json"
        output.write_text(json.dumps(payload), encoding="utf-8")
        call = ToolCall(
            run_id=run.id,
            tool_name=name,
            arguments_json=arguments,
            status="COMPLETED",
            agent_task_id=kwargs["agent_task_id"],
            approved_action_id=kwargs["approved_action_id"],
        )
        session.add(call)
        await session.flush()
        artifact = Artifact(run_id=run.id, tool_call_id=call.id, artifact_type="tool_output", file_path="oracle.json", sha256="a" * 64)
        session.add(artifact)
        await session.flush()
        session.add(Observation(run_id=run.id, tool_call_id=call.id, artifact_id=artifact.id, observation_type="tool_result", facts_json={}))
        await session.flush()
        return payload

    invoke.calls = calls
    return invoke


@pytest.mark.asyncio
async def test_compiled_exploit_action_dispatches_without_role_action(session_factory, tmp_path):
    async with session_factory() as session:
        challenge, run, _, approved, task, token = await _setup(session, tmp_path)
        invoker = _fake_invoker(tmp_path)
        result = await MultiAgentOrchestrator(tool_invoker=invoker).execute_compiled_action(session, run, challenge, None, task, approved)
        assert result.status == "COMPLETED", json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
        assert len(invoker.calls) == 1
        assert invoker.calls[0][0] == "sql_boolean_compare"
        assert invoker.calls[0][1] == approved.compiled_arguments_json
        assert task.status == "COMPLETED"
        assert approved.status == "CONSUMED"
        assert not await session.scalar(select(AgentTask).where(AgentTask.status == "RUNNING"))


@pytest.mark.asyncio
async def test_single_action_task_auto_completes(session_factory, tmp_path):
    async with session_factory() as session:
        challenge, run, _, _, task, _ = await _setup(session, tmp_path)
        result = await MultiAgentOrchestrator(tool_invoker=_fake_invoker(tmp_path)).execute_compiled_action(
            session, run, challenge, None, task, await session.get(ApprovedAction, task.context_json["approved_action_id"])
        )
        stored = await session.scalar(select(AgentTaskResult).where(AgentTaskResult.task_id == task.id))
        assert result.status == "COMPLETED", json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
        assert stored is not None and stored.status == "COMPLETED"


@pytest.mark.asyncio
async def test_sql_boolean_compare_compiled_schema_valid(session_factory, tmp_path):
    async with session_factory() as session:
        challenge, run, proposal, _, _, _ = await _setup(session, tmp_path)
        review = await session.scalar(select(AnalysisReview).where(AnalysisReview.proposal_id == proposal.id))
        compiled = await approved_action_compiler.compile(session, run, challenge, proposal, review, "sql_boolean_compare")
        assert load_tool_definitions()["sql_boolean_compare"].validate_arguments(compiled.arguments) == compiled.arguments
        assert "max_logical_calls" not in compiled.arguments


@pytest.mark.asyncio
async def test_compiled_action_not_dispatched_pauses_checkpoint(session_factory, tmp_path):
    async with session_factory() as session:
        challenge, run, _, approved, task, _ = await _setup(session, tmp_path)
        task.context_json = {"compiled_arguments_digest": "wrong"}
        result = await MultiAgentOrchestrator(tool_invoker=_fake_invoker(tmp_path)).execute_compiled_action(session, run, challenge, None, task, approved)
        assert result.status == "FAILED"
        assert task.status == "FAILED"
        assert approved.status == "REJECTED"
        assert run.status == "PAUSED_CHECKPOINT"
        assert run.last_error_code == "COMPILED_ACTION_NOT_DISPATCHED"


@pytest.mark.asyncio
async def test_boolean_result_promotes_capability(session_factory, tmp_path):
    async with session_factory() as session:
        challenge, run, proposal, _, task, _ = await _setup(session, tmp_path)
        approved = await session.scalar(select(ApprovedAction).where(ApprovedAction.run_id == run.id))
        result = await MultiAgentOrchestrator(tool_invoker=_fake_invoker(tmp_path)).execute_compiled_action(session, run, challenge, None, task, approved)
        fact = await session.scalar(select(VerifiedFact).where(VerifiedFact.source_task_id == task.id))
        review = AnalysisReview(
            proposal_id=proposal.id,
            task_kind="RESULT_REVIEW",
            decision="APPROVE",
            question_being_tested="Was the boolean oracle stable?",
            supporting_evidence_ids_json=result.evidence_ids,
            expected_true_signal_json={"matched": True},
            expected_false_signal_json={"matched": False},
            approved_fact_indexes_json=[0],
            next_phase="MAPPING",
        )
        session.add(review)
        await session.flush()
        await MultiAgentOrchestrator()._apply_result_review(session, run, task, review)
        state = await session.scalar(select(SolverState).where(SolverState.run_id == run.id))
        assert fact is not None and fact.promotion_status == "VERIFIED"
        assert "boolean_oracle_confirmed" in state.capability_ledger_json


@pytest.mark.asyncio
async def test_no_running_task_after_dispatch_failure(session_factory, tmp_path):
    async with session_factory() as session:
        challenge, run, _, approved, task, _ = await _setup(session, tmp_path)
        failure = {"status": "FAILED", "error_code": "RUNNER_UNAVAILABLE", "summary": "runner unavailable"}
        result = await MultiAgentOrchestrator(tool_invoker=_fake_invoker(tmp_path, result=failure)).execute_compiled_action(session, run, challenge, None, task, approved)
        assert result.status == "PARTIAL"
        assert task.status == "PARTIAL"
        assert not await session.scalar(select(AgentTask).where(AgentTask.status == "RUNNING"))


@pytest.mark.asyncio
async def test_dispatch_timeout_checkpoints_without_leaving_task_running(session_factory, tmp_path):
    async with session_factory() as session:
        challenge, run, _, approved, task, _ = await _setup(session, tmp_path)
        task.idle_deadline_at = datetime.now(UTC) + timedelta(seconds=0.05)

        async def never_dispatch(*args, **kwargs):
            await asyncio.Event().wait()

        result = await MultiAgentOrchestrator(tool_invoker=never_dispatch).execute_compiled_action(
            session, run, challenge, None, task, approved
        )
        assert result.status == "FAILED"
        assert task.status == "FAILED"
        assert approved.status == "REJECTED"
        assert run.last_error_code == "COMPILED_ACTION_NOT_DISPATCHED"
        assert not await session.scalar(select(AgentTask).where(AgentTask.status == "RUNNING"))
