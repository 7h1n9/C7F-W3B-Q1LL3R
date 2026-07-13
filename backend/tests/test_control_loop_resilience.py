from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.v1 import runs as runs_api
from app.models.base import Base
from app.models.challenge import Challenge
from app.models.run import Artifact, Observation, RunAttempt, RunEvent, SolveRun
from app.models.solver_state import SolverState
from app.orchestration.orchestrator import SolveOrchestrator
from app.services.progress_evaluator import progress_evaluator
from app.services.run_attempts import run_attempt_service


def test_codex_diagnostic_mode_accepts_unprefixed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import Settings

    monkeypatch.setenv("CODEX_DIAGNOSTIC_MODE", "true")
    assert Settings().codex_diagnostics_enabled is True


@pytest.mark.asyncio
async def test_repeated_completed_result_without_new_facts_is_no_progress(tmp_path: Path) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'progress.db'}", poolclass=StaticPool
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        challenge = Challenge(
            name="progress",
            target_url="http://target.test",
            allowed_hosts=["target.test"],
        )
        session.add(challenge)
        await session.flush()
        run = SolveRun(challenge_id=challenge.id, workspace_path=str(tmp_path))
        session.add(run)
        await session.flush()
        session.add(SolverState(run_id=run.id, current_phase="TESTING"))
        artifact1 = Artifact(run_id=run.id, artifact_type="http", file_path="responses/1.json")
        observation1 = Observation(
            run_id=run.id,
            artifact_id=artifact1.id,
            observation_type="http",
            summary="HTTP 200",
            facts_json={"tool_model_view": {"extracted_facts": {"status_code": 200, "final_url": "http://target.test/"}}},
        )
        session.add_all([artifact1, observation1])
        await session.commit()
        first = await progress_evaluator.evaluate(
            session,
            run,
            challenge,
            {"url": "http://target.test/"},
            "http_request",
            {"status": "COMPLETED"},
            observation1,
            artifact1,
        )
        artifact2 = Artifact(run_id=run.id, artifact_type="http", file_path="responses/2.json")
        observation2 = Observation(
            run_id=run.id,
            artifact_id=artifact2.id,
            observation_type="http",
            summary="HTTP 200",
            facts_json={"tool_model_view": {"extracted_facts": {"status_code": 200, "final_url": "http://target.test/"}}},
        )
        session.add_all([artifact2, observation2])
        await session.commit()
        second = await progress_evaluator.evaluate(
            session,
            run,
            challenge,
            {"url": "http://target.test/"},
            "http_request",
            {"status": "COMPLETED"},
            observation2,
            artifact2,
        )
        assert first["made_progress"] is True
        assert second["made_progress"] is False
        assert second["no_progress_count"] == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_startup_reconciler_closes_running_attempt_for_terminal_run(tmp_path: Path) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'attempts.db'}", poolclass=StaticPool
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        challenge = Challenge(name="attempt", target_url="http://target.test", allowed_hosts=["target.test"])
        session.add(challenge)
        await session.flush()
        run = SolveRun(
            challenge_id=challenge.id,
            workspace_path=str(tmp_path),
            status="COMPLETED_SOLVED",
            current_phase="COMPLETED_SOLVED",
            finished_at=datetime.now(UTC),
        )
        session.add(run)
        await session.flush()
        attempt = RunAttempt(
            run_id=run.id,
            attempt_number=1,
            engine_type="openai_compatible",
            started_at=datetime.now(UTC),
            status="RUNNING",
        )
        session.add(attempt)
        await session.commit()
        result = await run_attempt_service.reconcile_startup(session)
        refreshed = await session.scalar(select(RunAttempt).where(RunAttempt.id == attempt.id))
        assert result["attempts_closed"] == 1
        assert refreshed.status == "COMPLETED_SOLVED"
    await engine.dispose()


@pytest.mark.asyncio
async def test_run_attempt_begin_persists_lease_updated_at(tmp_path: Path) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'lease.db'}", poolclass=StaticPool
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        challenge = Challenge(
            name="lease",
            target_url="http://target.test",
            allowed_hosts=["target.test"],
        )
        session.add(challenge)
        await session.flush()
        run = SolveRun(challenge_id=challenge.id, workspace_path=str(tmp_path))
        session.add(run)
        await session.commit()
        attempt, lease = await run_attempt_service.begin(session, run)
        assert attempt.status == "RUNNING"
        assert lease.updated_at is not None
        await run_attempt_service.finish(session, run, attempt, lease)
    await engine.dispose()


@pytest.mark.asyncio
async def test_run_list_is_lightweight_for_codex_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'list.db'}", poolclass=StaticPool
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def fail_materialize(*_: object) -> None:
        raise AssertionError("list_runs must not materialize Codex runs")

    async def fail_diagnostics(*_: object) -> dict:
        raise AssertionError("list_runs must not run deep diagnostics")

    monkeypatch.setattr(runs_api.codex_materializer, "sync", fail_materialize)
    monkeypatch.setattr(runs_api.run_diagnostics_service, "analyze", fail_diagnostics)

    async with sessions() as session:
        challenge = Challenge(name="list", target_url="http://target.test", allowed_hosts=["target.test"])
        session.add(challenge)
        await session.flush()
        session.add(
            SolveRun(
                challenge_id=challenge.id,
                workspace_path=str(tmp_path),
                engine_type="codex_sdk",
                last_error_code="CODEX_NO_PROGRESS",
                last_error_message="paused",
            )
        )
        await session.commit()
        payload = await runs_api.list_runs(session)

    assert payload["data"][0].engine_type == "codex_sdk"
    assert payload["data"][0].diagnostic_tags == ["CODEX_NO_PROGRESS"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_codex_progress_snapshot_ignores_agent_only_events(tmp_path: Path) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'codex-progress.db'}", poolclass=StaticPool
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        challenge = Challenge(
            name="codex-progress",
            target_url="http://target.test",
            allowed_hosts=["target.test"],
        )
        session.add(challenge)
        await session.flush()
        run = SolveRun(
            challenge_id=challenge.id,
            workspace_path=str(tmp_path),
            engine_type="codex_sdk",
            status="PLANNING",
            current_phase="PLANNING",
        )
        session.add(run)
        await session.commit()

        orchestrator = SolveOrchestrator()
        before = await orchestrator._codex_progress_snapshot(session, run.id)
        session.add(
            RunEvent(
                run_id=run.id,
                sequence=1,
                event_type="agent.message",
                payload_json={"message": "[mock] Authorized workspace: Resume the authorized analysis."},
            )
        )
        await session.commit()
        after = await orchestrator._codex_progress_snapshot(session, run.id)

    assert after == before
    await engine.dispose()
