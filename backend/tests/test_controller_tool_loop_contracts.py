import pytest
from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.exceptions import DomainError
from app.models import Base
from app.models.challenge import Challenge
from app.models.multi_agent import AgentTask
from app.models.run import SolveRun
from app.orchestration.multi_agent_orchestrator import MultiAgentOrchestrator
from app.schemas.multi_agent import (
    AgentRole,
    AgentTaskContract,
    AgentTaskResultContract,
    AnalysisReviewContract,
    PlannerProposalContract,
    RoleAction,
    RoleFinishAction,
    RoleToolAction,
    TaskBudget,
)
from app.services.multi_agent import deterministic_controller
from app.services.tool_invocation_coordinator import ToolInvocationCoordinator
from app.tools.gateway import is_recon_sql_payload


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def test_role_action_is_one_discriminated_action() -> None:
    tool = TypeAdapter(RoleAction).validate_python({"type": "tool", "tool_name": "http_request", "arguments": {}, "purpose": "baseline", "expected_signal": {}})
    assert isinstance(tool, RoleToolAction)
    finish = TypeAdapter(RoleAction).validate_python({"type": "finish", "result": {"task_id": "AT-1", "status": "COMPLETED"}})
    assert isinstance(finish, RoleFinishAction)


def test_planner_and_analysis_prompt_contracts_are_directly_valid() -> None:
    proposal = PlannerProposalContract(proposal_id="PP-1", run_id="R-1", current_stage="INTAKE", decision_question="Which endpoint is public?", next_agent=AgentRole.RECON, objective="Map the public HTTP surface", success_condition="endpoint identified")
    review = AnalysisReviewContract(proposal_id=proposal.proposal_id, task_kind="PLAN_REVIEW", decision="APPROVE", question_being_tested=proposal.decision_question, expected_true_signal={"endpoint": True}, expected_false_signal={"endpoint": False})
    assert proposal.next_agent == AgentRole.RECON
    assert review.task_kind == "PLAN_REVIEW"


@pytest.mark.asyncio
async def test_invalid_planner_never_creates_fallback_proposal() -> None:
    run = type("Run", (), {"id": "R-1"})()
    task = type("Task", (), {"id": "AT-1"})()
    with pytest.raises(DomainError, match="PlannerProposalContract"):
        await MultiAgentOrchestrator()._proposal(None, run, task, AgentTaskResultContract(task_id="AT-1", status="COMPLETED", proposed_next_action={"raw": {}}))


def test_approved_constraints_and_recon_sql_boundary() -> None:
    checker = ToolInvocationCoordinator()
    assert checker._constraints_match({"field": "department"}, {"required": {"field": "department"}})
    assert not checker._constraints_match({"field": "asset_no"}, {"required": {"field": "department"}})
    assert not is_recon_sql_payload({"body": {"department": "sales"}})
    assert is_recon_sql_payload({"body": {"department": "x' OR 1=1 --"}})


@pytest.mark.asyncio
async def test_restart_reconciles_running_task_without_fact_promotion(session_factory) -> None:
    async with session_factory() as session:
        challenge = Challenge(name="recovery", target_url="http://target.test", allowed_hosts=["target.test"])
        session.add(challenge)
        await session.flush()
        run = SolveRun(challenge_id=challenge.id, workspace_path=".")
        session.add(run)
        await session.flush()
        await deterministic_controller.seed_policies(session)
        task = await deterministic_controller.create_task(session, AgentTaskContract(task_id="AT-RECOVERY", run_id=run.id, agent_role=AgentRole.RECON, objective="recon", allowed_tools=["http_request"], budget=TaskBudget(max_logical_calls=1, max_internal_requests=4, max_runtime_seconds=30)))
        await deterministic_controller.claim_task(session, task.id, "test", lease_seconds=30)
        result = await deterministic_controller.reconcile_startup(session)
        refreshed = await session.get(AgentTask, task.id)
        assert result["tasks_interrupted"] == 1
        assert refreshed.status == "INTERRUPTED"
