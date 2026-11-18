"""Cross-challenge lessons (CAP-11) and the fallback that delivers them."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.challenge import Challenge
from app.models.run import RunEvent, SolveRun, ToolCall
from app.services import knowledge_context
from app.services.challenge_lessons import (
    ChallengeLessonService,
    ProfileLessons,
    format_profile_lessons,
    profile_of,
)
from app.services.context_builder import _profile_prior
from app.services.knowledge_context import KnowledgeContextService

SQLI = "SQL_INJECTION"


@pytest.fixture()
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as opened:
        yield opened
    await engine.dispose()


async def _challenge(session, name: str, **metadata) -> Challenge:
    challenge = Challenge(
        name=name,
        challenge_type="WEB_TARGET",
        target_url="http://target.test/",
        allowed_hosts=["target.test"],
        metadata_json=dict(metadata),
    )
    session.add(challenge)
    await session.flush()
    return challenge


async def _solved_run(session, challenge: Challenge, *, tool: str = "http_request", args: dict | None = None) -> SolveRun:
    run = SolveRun(
        challenge_id=challenge.id,
        workspace_path=".",
        solver_mode="muteki",
        status="COMPLETED_SOLVED",
    )
    session.add(run)
    await session.flush()
    session.add(
        ToolCall(
            run_id=run.id,
            tool_name=tool,
            arguments_json=args or {"url": "http://target.test/api/warranty/check", "body": "x"},
            status="COMPLETED",
        )
    )
    session.add(
        RunEvent(
            run_id=run.id,
            sequence=1,
            event_type="tool.completed",
            payload_json={"tool": tool},
        )
    )
    await session.commit()
    return run


# --------------------------------------------------------------------------- aggregation


async def test_profile_lessons_pool_other_challenges_of_the_same_type(session) -> None:
    solved = await _challenge(session, "old-sqli", vulnerability_type=SQLI, target_stack=["flask"])
    new = await _challenge(session, "new-sqli", vulnerability_type=SQLI, target_stack=["flask"])
    await _solved_run(session, solved, tool="http_session_request")

    profile = await ChallengeLessonService().extract_by_profile(
        session,
        vulnerability_type=SQLI,
        target_stack=["flask"],
        exclude_challenge_id=new.id,
    )

    assert profile.run_count == 1
    assert profile.challenge_count == 1
    assert profile.usable is True
    assert "http_session_request" in profile.lessons["effective_tool_order"]


async def test_a_different_vulnerability_type_is_never_pooled(session) -> None:
    other = await _challenge(session, "ssrf-case", vulnerability_type="SSRF")
    await _solved_run(session, other, tool="http_request")

    profile = await ChallengeLessonService().extract_by_profile(session, vulnerability_type=SQLI)

    assert profile.run_count == 0
    assert profile.usable is False


async def test_target_stack_filters_the_profile(session) -> None:
    flask_case = await _challenge(session, "flask-sqli", vulnerability_type=SQLI, target_stack=["flask"])
    node_case = await _challenge(session, "node-sqli", vulnerability_type=SQLI, target_stack=["node"])
    await _solved_run(session, flask_case)
    await _solved_run(session, node_case)

    profile = await ChallengeLessonService().extract_by_profile(
        session, vulnerability_type=SQLI, target_stack=["flask"]
    )

    assert profile.run_count == 1
    assert profile.challenge_count == 1


async def test_an_empty_stack_matches_every_type_member(session) -> None:
    await _solved_run(session, await _challenge(session, "a", vulnerability_type=SQLI, target_stack=["flask"]))
    await _solved_run(session, await _challenge(session, "b", vulnerability_type=SQLI, target_stack=["node"]))

    profile = await ChallengeLessonService().extract_by_profile(session, vulnerability_type=SQLI)

    assert profile.run_count == 2


async def test_the_current_challenge_is_excluded_from_its_own_profile(session) -> None:
    current = await _challenge(session, "current", vulnerability_type=SQLI)
    await _solved_run(session, current)

    profile = await ChallengeLessonService().extract_by_profile(
        session, vulnerability_type=SQLI, exclude_challenge_id=current.id
    )

    assert profile.run_count == 0


async def test_a_challenge_without_a_profile_cannot_join_one(session) -> None:
    unlabelled = await _challenge(session, "unlabelled")
    await _solved_run(session, unlabelled)

    profile = await ChallengeLessonService().extract_by_profile(session, vulnerability_type=SQLI)

    assert profile.run_count == 0


async def test_only_solved_runs_are_aggregated(session) -> None:
    challenge = await _challenge(session, "unsolved", vulnerability_type=SQLI)
    unsolved = SolveRun(
        challenge_id=challenge.id,
        workspace_path=".",
        solver_mode="muteki",
        status="COMPLETED_UNSOLVED",
    )
    session.add(unsolved)
    await session.commit()

    profile = await ChallengeLessonService().extract_by_profile(session, vulnerability_type=SQLI)

    assert profile.run_count == 0


async def test_same_challenge_extract_still_returns_only_the_five_lesson_keys(session) -> None:
    challenge = await _challenge(session, "solo", vulnerability_type=SQLI)
    await _solved_run(session, challenge)

    lessons = await ChallengeLessonService().extract(session, challenge.id)

    assert set(lessons) == {
        "effective_tool_order",
        "verified_attack_surface",
        "common_blockers",
        "argument_shapes",
        "avoid_repeated_behaviors",
    }


async def test_argument_shapes_use_the_shared_vocabulary(session) -> None:
    challenge = await _challenge(session, "shape", vulnerability_type=SQLI)
    await _solved_run(session, challenge, tool="http_request", args={"url": "http://t/", "body": "x"})

    lessons = await ChallengeLessonService().extract(session, challenge.id)

    # ``argument_shape`` strips the ctfctl. prefix and sorts the key set.
    assert lessons["argument_shapes"] == ["http_request{body,url}"]


# --------------------------------------------------------------------------- rendering


def test_format_labels_the_block_as_self_produced() -> None:
    profile = ProfileLessons(
        lessons={"effective_tool_order": ["http_request", "jwt_sign"]},
        vulnerability_type=SQLI,
        challenge_count=2,
        run_count=3,
    )
    text = format_profile_lessons(profile)

    assert "SQL_INJECTION" in text
    assert "自产先验" in text
    assert "http_request, jwt_sign" in text


def test_format_is_empty_when_there_is_nothing_to_say() -> None:
    assert format_profile_lessons(ProfileLessons(vulnerability_type=SQLI)) == ""
    assert format_profile_lessons(ProfileLessons(lessons={"effective_tool_order": ["x"]})) == ""
    empty = ProfileLessons(lessons={"effective_tool_order": ["x"]}, vulnerability_type=SQLI, run_count=1, challenge_count=1)
    assert format_profile_lessons(empty, max_chars=0) == ""


def test_format_respects_its_character_cap() -> None:
    profile = ProfileLessons(
        lessons={"effective_tool_order": [f"tool-{index}" for index in range(40)]},
        vulnerability_type=SQLI,
        challenge_count=1,
        run_count=1,
    )
    text = format_profile_lessons(profile, max_chars=120)

    assert len(text) <= 120
    assert text.endswith("[profile prior truncated]")


def test_profile_of_reads_only_declared_metadata() -> None:
    assert profile_of(SimpleNamespace(metadata_json={"vulnerability_type": "sqli", "target_stack": ["flask"]})) == (
        "SQLI",
        ("flask",),
    )
    assert profile_of(SimpleNamespace(metadata_json=None)) == ("", ())
    assert profile_of(SimpleNamespace()) == ("", ())


# --------------------------------------------------------------------------- delivery


async def test_the_profile_prior_is_only_used_when_there_is_no_same_challenge_history(session) -> None:
    current = await _challenge(session, "current", vulnerability_type=SQLI)
    other = await _challenge(session, "other", vulnerability_type=SQLI)
    await _solved_run(session, other)

    empty = await _profile_prior(session, current, {"effective_tool_order": []})
    assert "SQL_INJECTION" in empty

    # History of its own always wins: the profile would dilute a precise prior.
    filled = await _profile_prior(session, current, {"effective_tool_order": ["http_request"]})
    assert filled == ""


async def test_a_challenge_without_a_type_gets_no_profile_prior(session) -> None:
    unlabelled = await _challenge(session, "unlabelled")
    assert await _profile_prior(session, unlabelled, {}) == ""


def _run(hints: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(hints_json=hints or {"knowledge_mode": "on"})


def _challenge_for_prior() -> SimpleNamespace:
    return SimpleNamespace(
        name="Generic web console",
        description="An authorized lab web application.",
        challenge_type="WEB_TARGET",
        target_url="http://example.test/app",
        allowed_hosts=["example.test"],
        metadata_json={},
    )


def test_a_supplied_profile_prior_reaches_the_worker_block(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("C7F_KNOWLEDGE_MODE", "on")
    monkeypatch.delenv("C7F_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.setenv("C7F_KNOWLEDGE_TEMPLATES_ROOT", str(tmp_path / "missing"))
    knowledge_context.clear_retriever_cache()
    block = format_profile_lessons(
        ProfileLessons(
            lessons={"effective_tool_order": ["http_request"]},
            vulnerability_type=SQLI,
            challenge_count=1,
            run_count=1,
        )
    )

    result = KnowledgeContextService.build_worker_prior(
        _run(),
        _challenge_for_prior(),
        intent_goal="Probe the search parameter for SQL injection",
        profile_prior=block,
    )

    assert result["enabled"] is True
    assert result["profile_prior"] == block
    assert block in result["prior"]
    assert len(result["prior"]) <= knowledge_context._MAX_WORKER_PRIOR_CHARS


def test_no_profile_prior_leaves_the_worker_block_alone(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("C7F_KNOWLEDGE_MODE", "on")
    monkeypatch.delenv("C7F_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.setenv("C7F_KNOWLEDGE_TEMPLATES_ROOT", str(tmp_path / "missing"))
    knowledge_context.clear_retriever_cache()

    result = KnowledgeContextService.build_worker_prior(
        _run(),
        _challenge_for_prior(),
        intent_goal="Probe the search parameter for SQL injection",
    )

    assert result["profile_prior"] == ""
