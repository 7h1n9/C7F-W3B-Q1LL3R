"""A completed Muteki Run must stay exportable after the native handoff.

Regression cover for the defect "任务完成后 PoC 和 Writeup 无法导出": a Run that
went terminal through ``reconcile_muteki_native_terminal`` never ran the
in-process report adapter, so it had no ACTIVE report artifact, no
``final/writeup.md`` and no ``report_json.flag_verified`` — the Writeup endpoint
answered 404 and the PoC endpoint answered 409 on a genuinely solved Run.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.v1.runs import get_report
from app.models import Base
from app.models.challenge import Challenge
from app.models.run import Artifact, FlagCandidate, RunEvent, SolveRun
from app.services.reports import ensure_muteki_report, recover_muteki_terminal_result
from app.services.run_supervisor import RunSupervisor

FLAG = "flag{recovered_by_terminal_handoff}"


async def _session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _muteki_run(session, workspace) -> SolveRun:
    challenge = Challenge(
        name="Recovered Muteki export",
        description="Solved through the native terminal handoff.",
        challenge_type="WEB_TARGET",
        target_url="http://target.test/",
        allowed_hosts=["target.test"],
        flag_pattern=r"flag\{[^}]+\}",
    )
    session.add(challenge)
    await session.flush()
    run = SolveRun(
        challenge_id=challenge.id,
        workspace_path=str(workspace),
        solver_mode="muteki",
        status="RUNNING",
        current_phase="COORDINATOR",
    )
    session.add(run)
    await session.flush()
    return run


def _terminal_event(run_id: str, *, flag_found: bool, reason: str) -> RunEvent:
    return RunEvent(
        run_id=run_id,
        sequence=1,
        event_type="muteki.run_finished",
        payload_json={
            "muteki_event_type": "run_finished",
            "payload": {"reason": reason, "flag_found": flag_found, "verified_flag_count": 1 if flag_found else 0},
        },
    )


@pytest.mark.asyncio
async def test_recovered_result_prefers_a_verified_candidate(tmp_path) -> None:
    engine, sessions = await _session_factory()
    async with sessions() as session:
        run = await _muteki_run(session, tmp_path)
        session.add(
            FlagCandidate(
                run_id=run.id,
                candidate=FLAG,
                verified=True,
                review_state="VALID",
            )
        )
        await session.commit()

        result = await recover_muteki_terminal_result(session, run)

        assert result.flag == FLAG
        assert result.flag_found is True
        assert result.status == "RUNNING"
    await engine.dispose()


@pytest.mark.asyncio
async def test_recovered_result_never_invents_a_placeholder_flag(tmp_path) -> None:
    engine, sessions = await _session_factory()
    async with sessions() as session:
        run = await _muteki_run(session, tmp_path)
        session.add(FlagCandidate(run_id=run.id, candidate="flag{...}", verified=True, review_state="VALID"))
        await session.commit()

        result = await recover_muteki_terminal_result(session, run)

        assert result.flag is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_ensure_muteki_report_is_idempotent_and_skips_non_muteki(tmp_path) -> None:
    engine, sessions = await _session_factory()
    async with sessions() as session:
        run = await _muteki_run(session, tmp_path)
        session.add(FlagCandidate(run_id=run.id, candidate=FLAG, verified=True, review_state="VALID"))
        run.status = "COMPLETED_SOLVED"
        await session.commit()

        assert await ensure_muteki_report(session, run) is True
        first = await session.scalar(
            select(Artifact.id).where(Artifact.run_id == run.id, Artifact.artifact_type == "report")
        )
        assert await ensure_muteki_report(session, run) is True
        artifacts = list(
            (await session.scalars(select(Artifact).where(Artifact.run_id == run.id))).all()
        )
        assert len([item for item in artifacts if item.artifact_type == "report"]) == 1
        assert first is not None
    await engine.dispose()


@pytest.mark.asyncio
async def test_terminal_reconciled_run_exports_writeup_and_passes_poc_gate(tmp_path) -> None:
    engine, sessions = await _session_factory()
    async with sessions() as session:
        run = await _muteki_run(session, tmp_path)
        session.add(_terminal_event(run.id, flag_found=True, reason="FLAG_VERIFIED"))
        session.add(FlagCandidate(run_id=run.id, candidate=FLAG, verified=True, review_state="VALID"))
        await session.commit()

        outcome = await RunSupervisor().reconcile_muteki_native_terminal(session, run)
        assert outcome is not None
        assert outcome.status == "COMPLETED_SOLVED"
        await session.refresh(run)
        assert run.report_json is None or run.report_json.get("flag_verified") is not True

        payload = await get_report(str(run.id), session)

        data = payload["data"]
        assert FLAG in data["content"]
        assert data["report_json"]["flag_verified"] is True
        assert data["report_json"]["flag"] == FLAG
        assert (tmp_path / "final" / "writeup.md").is_file()
        assert (tmp_path / "final" / "report.json").is_file()

        artifact = await session.scalar(
            select(Artifact).where(
                Artifact.run_id == run.id,
                Artifact.artifact_type == "report",
                Artifact.status == "ACTIVE",
            )
        )
        assert artifact is not None
        await session.refresh(run)
        assert str(run.report_json.get("flag_verified")) == "True"
    await engine.dispose()


@pytest.mark.asyncio
async def test_unsolved_reconciled_run_still_publishes_a_writeup(tmp_path) -> None:
    engine, sessions = await _session_factory()
    async with sessions() as session:
        run = await _muteki_run(session, tmp_path)
        session.add(_terminal_event(run.id, flag_found=False, reason="NO_VERIFIED_FLAG"))
        await session.commit()

        outcome = await RunSupervisor().reconcile_muteki_native_terminal(session, run)
        assert outcome is not None
        assert outcome.status == "COMPLETED_UNSOLVED"
        await session.refresh(run)

        payload = await get_report(str(run.id), session)

        assert payload["data"]["content"].strip()
        assert payload["data"]["report_json"]["flag_verified"] is False
    await engine.dispose()
