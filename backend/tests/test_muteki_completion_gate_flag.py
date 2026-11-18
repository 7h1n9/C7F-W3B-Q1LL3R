"""A solved native Muteki Run must surface its gate-verified flag as a candidate.

Regression cover for "为什么判断已解出，但是 flag 候选不显示东西": the native
Muteki path records the flag in ``report_json`` (``flag`` + ``flag_verified``)
but never went through the tool gateway that called ``extract_candidates``.  No
``flag_candidates`` row was ever written, so the Result panel showed
"FLAG EVENT RECORDED" with an empty candidate list on a genuinely solved Run.

The banner here is deliberately NOT ``flag{...}`` — the original defect was
masked for default-banner challenges and only visible for CTF2/DASCTF banners.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.challenge import Challenge
from app.models.run import FlagCandidate, FlagProvenance, SolveRun
from app.services.flags import flag_service

BANNER_FLAG = "CTF2{1aa54529-0abd-45f1-85f8-29cafcbea2ad}"


async def _session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _muteki_run(session, *, status: str, report_json: dict) -> SolveRun:
    challenge = Challenge(
        name="WriteSHell",
        description="Completion-Gate flag projection.",
        challenge_type="WEB_TARGET",
        target_url="http://target.test/",
        allowed_hosts=["target.test"],
        flag_pattern=r"DASCTF\{[^}]+\}",
    )
    session.add(challenge)
    await session.flush()
    run = SolveRun(
        challenge_id=challenge.id,
        workspace_path="/tmp/workspace",
        solver_mode="muteki",
        status=status,
        current_phase="REPORTING",
        report_json=report_json,
    )
    session.add(run)
    await session.flush()
    return run


@pytest.mark.asyncio
async def test_completion_gate_flag_becomes_a_valid_candidate() -> None:
    engine, sessions = await _session_factory()
    async with sessions() as session:
        run = await _muteki_run(
            session,
            status="COMPLETED_SOLVED",
            report_json={"flag": BANNER_FLAG, "flag_verified": True},
        )
        await session.commit()

        item = await flag_service.materialize_completion_gate_flag(session, run)
        assert item is not None
        assert item.candidate == BANNER_FLAG
        assert item.review_state == "VALID"
        assert item.verified is True
        # A CTF2/DASCTF banner is not the declared DASCTF pattern, but the gate
        # verified it, so it must still land as a valid candidate.
        assert item.first_seen_source_type == "COMPLETION_GATE"

        provenance = await session.scalar(
            select(FlagProvenance).where(FlagProvenance.candidate_id == item.id)
        )
        assert provenance is not None
        assert provenance.verification_source_type == "COMPLETION_GATE"
    await engine.dispose()


@pytest.mark.asyncio
async def test_materialize_is_idempotent() -> None:
    engine, sessions = await _session_factory()
    async with sessions() as session:
        run = await _muteki_run(
            session,
            status="COMPLETED_SOLVED",
            report_json={"flag": BANNER_FLAG, "flag_verified": True},
        )
        await session.commit()

        first = await flag_service.materialize_completion_gate_flag(session, run)
        second = await flag_service.materialize_completion_gate_flag(session, run)
        assert first is not None and second is not None
        assert first.id == second.id

        rows = list((await session.scalars(select(FlagCandidate).where(FlagCandidate.run_id == run.id))).all())
        assert len(rows) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_materialize_skips_unverified_or_placeholder_flags() -> None:
    engine, sessions = await _session_factory()
    async with sessions() as session:
        unverified = await _muteki_run(
            session, status="COMPLETED_UNSOLVED", report_json={"flag": None, "flag_verified": False}
        )
        await session.commit()
        assert await flag_service.materialize_completion_gate_flag(session, unverified) is None

        # A templated placeholder must never be laundered into a solved Run.
        placeholder = await _muteki_run(
            session, status="COMPLETED_SOLVED", report_json={"flag": "DASCTF{...}", "flag_verified": True}
        )
        await session.commit()
        assert await flag_service.materialize_completion_gate_flag(session, placeholder) is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_materialize_does_not_resurrect_a_rejected_candidate() -> None:
    engine, sessions = await _session_factory()
    async with sessions() as session:
        run = await _muteki_run(
            session,
            status="COMPLETED_SOLVED",
            report_json={"flag": BANNER_FLAG, "flag_verified": True},
        )
        session.add(
            FlagCandidate(
                run_id=run.id,
                candidate=BANNER_FLAG,
                verified=False,
                review_state="INVALID",
                first_seen_source_type="TOOL_ARTIFACT",
            )
        )
        await session.commit()

        item = await flag_service.materialize_completion_gate_flag(session, run)
        assert item is not None
        # The operator's rejection stands; the projection must not override it.
        assert item.review_state == "INVALID"
        assert item.verified is False
    await engine.dispose()


@pytest.mark.asyncio
async def test_reconcile_materializes_the_flag_and_marks_solved() -> None:
    engine, sessions = await _session_factory()
    async with sessions() as session:
        run = await _muteki_run(
            session,
            status="COMPLETED_SOLVED",
            report_json={"flag": BANNER_FLAG, "flag_verified": True},
        )
        await session.commit()

        changed = await flag_service.reconcile_run_status(session, run)
        # The Run was already terminal, but reconcile also promotes the parent
        # Challenge to SOLVED, so a change IS reported. The point of this test
        # is that a VALID candidate now exists where previously there was none.
        assert changed is True
        rows = list((await session.scalars(select(FlagCandidate).where(FlagCandidate.run_id == run.id))).all())
        assert [row.candidate for row in rows] == [BANNER_FLAG]
        assert rows[0].review_state == "VALID"

        challenge = await session.get(Challenge, run.challenge_id)
        assert challenge is not None and challenge.status == "SOLVED"
    await engine.dispose()
