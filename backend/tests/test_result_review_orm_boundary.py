"""Regression tests for ID-only ResultReview continuation boundaries."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.challenge import Challenge
from app.models.multi_agent import AgentTask, AnalysisReview, PlannerProposal, VerifiedFact
from app.models.run import RunContinuation, SolveRun
from app.orchestration.multi_agent_orchestrator import MultiAgentOrchestrator
from app.services.continuations import CONTINUATION_RUNNING, continuation_service


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    # Keep expiry enabled here deliberately.  This reproduces the runtime
    # boundary that used to make post-commit ORM access raise MissingGreenlet.
    factory = async_sessionmaker(engine, expire_on_commit=True)
    async with factory() as session:
        challenge = Challenge(name="ORM boundary", target_url="http://target.test", allowed_hosts=["target.test"])
        session.add(challenge)
        await session.flush()
        run = SolveRun(challenge_id=challenge.id, workspace_path=".")
        session.add(run)
        await session.flush()
        run_id = str(run.id)
        await session.commit()
    yield factory, run_id
    await engine.dispose()


@pytest.mark.asyncio
async def test_claim_returns_post_commit_snapshot_not_expired_orm(session_factory):
    factory, run_id = session_factory
    async with factory() as session:
        item = await continuation_service.request(
            session,
            run_id,
            kind="RESULT_REVIEW_PENDING",
            dedupe_key="review:boundary:1",
            payload={"producing_task_id": "producer-1"},
        )
        continuation_id = str(item.id)
        await session.commit()

        claimed = await continuation_service.claim(session, continuation_id)
        assert claimed is not None
        assert claimed["id"] == continuation_id
        assert claimed["run_id"] == run_id
        assert claimed["kind"] == "RESULT_REVIEW_PENDING"
        assert claimed["status"] == CONTINUATION_RUNNING
        assert claimed["payload"] == {"producing_task_id": "producer-1"}


@pytest.mark.asyncio
async def test_result_review_continuation_can_cross_sessions_by_ids(session_factory):
    factory, run_id = session_factory
    async with factory() as first_session:
        item = await continuation_service.request(
            first_session,
            run_id,
            kind="RESULT_REVIEW_PENDING",
            dedupe_key="review:boundary:2",
            payload={"producing_task_id": "producer-2"},
        )
        continuation_id = str(item.id)
        await first_session.commit()
        claimed = await continuation_service.claim(first_session, continuation_id)

    assert claimed is not None
    # The continuation payload is plain data after the first session closes.
    async with factory() as second_session:
        persisted = await second_session.get(RunContinuation, claimed["id"])
        run = await second_session.get(SolveRun, claimed["run_id"])
        assert persisted is not None
        assert run is not None
        assert persisted.status == CONTINUATION_RUNNING
        assert claimed["payload"]["producing_task_id"] == "producer-2"


@pytest.mark.asyncio
async def test_candidate_review_promotion_regression_is_covered_by_existing_gate(session_factory):
    """A reloaded ResultReview can still promote a durable candidate fact."""
    factory, run_id = session_factory
    async with factory() as session:
        run = await session.get(SolveRun, run_id)
        assert run is not None
        proposal = PlannerProposal(
            run_id=run.id,
            proposal_id="PP-ORM-BOUNDARY",
            current_stage="BUSINESS_BASELINE",
            next_agent="RECON",
            objective="Promote the candidate fact",
            success_condition="candidate promoted",
        )
        session.add(proposal)
        await session.flush()
        task = AgentTask(
            run_id=run.id,
            agent_role="RECON",
            task_kind="RECON",
            objective="Produce candidate",
            status="COMPLETED",
        )
        session.add(task)
        await session.flush()
        fact = VerifiedFact(
            run_id=run.id,
            fact_key="test.candidate",
            fact_type="GENERAL",
            value_json={"ok": True},
            source_task_id=task.id,
            promotion_status="CANDIDATE",
        )
        session.add(fact)
        await session.flush()
        fact_id = str(fact.id)
        review = AnalysisReview(
            proposal_id=proposal.id,
            task_kind="RESULT_REVIEW",
            decision="APPROVE",
            approved_fact_indexes_json=[0],
            next_phase="BUSINESS_BASELINE",
        )
        session.add(review)
        await session.flush()

        reloaded_run = await session.get(SolveRun, run_id)
        reloaded_task = await session.get(AgentTask, task.id)
        assert reloaded_run is not None and reloaded_task is not None
        promoted = await MultiAgentOrchestrator()._apply_result_review(
            session, reloaded_run, reloaded_task, review
        )
        await session.commit()
        persisted = await session.get(VerifiedFact, fact_id)
        assert promoted == [fact_id]
        assert persisted is not None and persisted.promotion_status == "VERIFIED"
