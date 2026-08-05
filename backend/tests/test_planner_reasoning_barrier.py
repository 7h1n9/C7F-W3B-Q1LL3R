import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.challenge import Challenge
from app.models.multi_agent import AgentTask, VerifiedFact
from app.models.run import RunContinuation, SolveRun
from app.orchestration.multi_agent_orchestrator import multi_agent_orchestrator
from app.services.solver_state import solver_state_service


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_planner_barrier_blocks_candidate_with_pending_result_review(session_factory):
    async with session_factory() as session:
        challenge = Challenge(name="asset", target_url="http://asset.local", allowed_hosts=["asset.local"], challenge_type="WEB_TARGET")
        session.add(challenge)
        await session.flush()
        run = SolveRun(challenge_id=challenge.id, workspace_path=".", role_snapshot_json={}, status="EVALUATING")
        session.add(run)
        await session.flush()
        await solver_state_service.initialize(session, run, "WEB_TARGET", [], "asset", "asset")
        producer = AgentTask(run_id=run.id, agent_role="RECON", task_kind="RECON", objective="baseline", status="COMPLETED")
        session.add(producer)
        await session.flush()
        session.add(AgentTask(run_id=run.id, agent_role="ANALYSIS", task_kind="RESULT_REVIEW", objective="review", created_by_task_id=producer.id, status="PENDING"))
        session.add(VerifiedFact(run_id=run.id, fact_key="asset_warranty.valid_baseline", fact_type="BUSINESS_RESPONSE_BASELINE", value_json={}, promotion_status="CANDIDATE", source_task_id=producer.id))
        session.add(RunContinuation(run_id=run.id, kind="RESULT_REVIEW_PENDING", dedupe_key="review:baseline", status="PENDING", payload_json={}))
        await session.commit()

        allowed, reason = await multi_agent_orchestrator.can_dispatch_planner(session, run)

        assert allowed is False
        assert reason == "RESULT_REVIEW_PENDING"


@pytest.mark.asyncio
async def test_planner_barrier_opens_after_review_and_promotion(session_factory):
    async with session_factory() as session:
        challenge = Challenge(name="asset", target_url="http://asset.local", allowed_hosts=["asset.local"], challenge_type="WEB_TARGET")
        session.add(challenge)
        await session.flush()
        run = SolveRun(challenge_id=challenge.id, workspace_path=".", role_snapshot_json={}, status="PLANNING")
        session.add(run)
        await session.flush()
        await solver_state_service.initialize(session, run, "WEB_TARGET", [], "asset", "asset")
        session.add(AgentTask(run_id=run.id, agent_role="ANALYSIS", task_kind="RESULT_REVIEW", objective="review", status="COMPLETED"))
        session.add(VerifiedFact(run_id=run.id, fact_key="asset_warranty.valid_baseline", fact_type="BUSINESS_RESPONSE_BASELINE", value_json={}, promotion_status="VERIFIED"))
        session.add(RunContinuation(run_id=run.id, kind="RESULT_REVIEW_PENDING", dedupe_key="review:baseline", status="COMPLETED", payload_json={}))
        await session.commit()

        allowed, reason = await multi_agent_orchestrator.can_dispatch_planner(session, run)

        assert allowed is True
        assert reason == "READY"
