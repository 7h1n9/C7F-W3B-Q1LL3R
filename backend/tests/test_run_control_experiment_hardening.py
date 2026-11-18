"""Hardening regressions for run control experiment pairing."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.exceptions import DomainError
from app.models.base import Base
from app.models.challenge import Challenge
from app.models.run import SolveRun
from app.models.skill import RunSkillSnapshot, Skill
from app.schemas.run import RunControlExperimentCreate
from app.services.run_control_experiment import create_control_experiment


@pytest.fixture
async def session_factory(
    tmp_path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_path = tmp_path / "run-control-hardening.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    yield async_sessionmaker(engine, expire_on_commit=False)

    await engine.dispose()


async def _create_source(
    session: AsyncSession,
    *,
    knowledge_mode: str | None = "off",
    status: str = "COMPLETED_SOLVED",
) -> SolveRun:
    challenge = Challenge(
        name="run control hardening",
        target_url="http://target.test",
        allowed_hosts=["target.test"],
        challenge_type="WEB_TARGET",
        # Pairing requires an evaluator-backed challenge (see CAP-01).
        metadata_json={"benchmark_case_id": "sql-injection-boolean"},
    )
    session.add(challenge)
    await session.flush()

    hints: dict = {"task_setting": "copy-me"}
    if knowledge_mode is not None:
        hints["knowledge_mode"] = knowledge_mode

    source = SolveRun(
        challenge_id=challenge.id,
        engine_type="openai_compatible",
        solver_mode="muteki",
        model_config_id="worker-config",
        role_name="web",
        role_version="7",
        role_snapshot_json={"profile": "web"},
        hints_json=hints,
        status=status,
        workspace_path="source-workspace",
        max_agent_steps=73,
        max_tool_calls=42,
        max_context_observations=11,
        max_runtime_seconds=321,
        max_total_runtime_seconds=1234,
        agent_checkpoint_interval=17,
        controller_revision=3,
        assistance_level="GUIDED",
    )
    session.add(source)
    await session.commit()
    return source


class _FakeRunMethods:
    def __init__(self, *, block_create: bool = False) -> None:
        self.block_create = block_create
        self.create_entered = asyncio.Event()
        self.second_create_entered = asyncio.Event()
        self.release_create = asyncio.Event()
        self.create_calls = 0
        self.started_run_ids: list[str] = []

    async def create_run(
        self,
        challenge_id: str,
        payload,
        session: AsyncSession,
    ) -> dict:
        self.create_calls += 1
        if self.create_calls == 1:
            self.create_entered.set()
        else:
            self.second_create_entered.set()

        if self.block_create:
            await self.release_create.wait()

        control = SolveRun(
            challenge_id=challenge_id,
            engine_type=payload.engine_type,
            solver_mode=payload.solver_mode,
            model_config_id=payload.model_config_id,
            role_snapshot_json={"placeholder": True},
            hints_json={"knowledge_mode": "placeholder"},
            workspace_path=f"control-workspace-{self.create_calls}",
            max_agent_steps=payload.max_agent_steps,
            max_tool_calls=payload.max_tool_calls,
            max_context_observations=payload.max_context_observations,
            max_runtime_seconds=payload.max_runtime_seconds,
            max_total_runtime_seconds=payload.max_total_runtime_seconds,
        )
        session.add(control)
        await session.commit()
        return {"data": SimpleNamespace(id=control.id)}

    async def start_run(self, run_id: str, _session: AsyncSession) -> dict:
        self.started_run_ids.append(run_id)
        return {"data": {"run_id": run_id, "status": "STARTING"}}

    @staticmethod
    def read_run(run: SolveRun) -> SolveRun:
        return run


@pytest.mark.asyncio
async def test_concurrent_creation_allows_only_one_control(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        source = await _create_source(session)
        source_id = source.id
        challenge_id = source.challenge_id

    methods = _FakeRunMethods(block_create=True)
    payload = RunControlExperimentCreate(
        factor="knowledge_mode",
        counterfactual_value="on",
        start=False,
    )

    async def attempt() -> dict:
        async with session_factory() as session:
            loaded = await session.get(SolveRun, source_id)
            assert loaded is not None
            return await create_control_experiment(
                session,
                loaded,
                payload,
                create_run_fn=methods.create_run,
                start_run_fn=methods.start_run,
                read_run_fn=methods.read_run,
            )

    first = asyncio.create_task(attempt())
    await asyncio.wait_for(methods.create_entered.wait(), timeout=2)

    second = asyncio.create_task(attempt())
    await asyncio.sleep(0.05)
    overlapped_before_commit = methods.second_create_entered.is_set()

    methods.release_create.set()
    results = await asyncio.wait_for(
        asyncio.gather(first, second, return_exceptions=True),
        timeout=5,
    )

    successes = [item for item in results if isinstance(item, dict)]
    failures = [item for item in results if isinstance(item, BaseException)]

    assert overlapped_before_commit is False
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], DomainError)
    assert failures[0].code == "RUN_ALREADY_IN_CONTROL_EXPERIMENT"
    assert methods.create_calls == 1

    async with session_factory() as session:
        runs = list(
            (
                await session.scalars(
                    select(SolveRun).where(
                        SolveRun.challenge_id == challenge_id
                    )
                )
            ).all()
        )

    assert len(runs) == 2
    assert sum(run.id == source_id for run in runs) == 1
    assert sum(run.id != source_id for run in runs) == 1
    control = next(run for run in runs if run.id != source_id)
    assert control.control_source_run_id == source_id


@pytest.mark.asyncio
async def test_runtime_default_on_is_persisted_and_control_is_off(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("C7F_KNOWLEDGE_MODE", "on")
    methods = _FakeRunMethods()

    async with session_factory() as session:
        source = await _create_source(session, knowledge_mode=None)
        assert "knowledge_mode" not in source.hints_json

        result = await create_control_experiment(
            session,
            source,
            RunControlExperimentCreate(
                factor="knowledge_mode",
                counterfactual_value="off",
                start=False,
            ),
            create_run_fn=methods.create_run,
            start_run_fn=methods.start_run,
            read_run_fn=methods.read_run,
        )

        await session.refresh(source)
        control = await session.get(
            SolveRun,
            result["data"]["control_run"].id,
        )

    assert control is not None
    assert control.control_source_run_id == source.id
    assert source.hints_json["knowledge_mode"] == "on"
    assert control.hints_json["knowledge_mode"] == "off"
    assert source.hints_json["ab_experiment"]["role"] == "treatment"
    assert control.hints_json["ab_experiment"]["role"] == "baseline"
    assert methods.started_run_ids == []


@pytest.mark.asyncio
async def test_control_does_not_copy_evaluation_report_from_source(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    methods = _FakeRunMethods()
    evaluation_report = {
        "final_result": "SUCCESS",
        "summary": "source result must remain source-only",
    }

    async with session_factory() as session:
        source = await _create_source(session)
        source.hints_json = {
            **source.hints_json,
            "evaluation_report": evaluation_report,
        }
        await session.commit()

        result = await create_control_experiment(
            session,
            source,
            RunControlExperimentCreate(
                factor="knowledge_mode",
                counterfactual_value="on",
                start=False,
            ),
            create_run_fn=methods.create_run,
            start_run_fn=methods.start_run,
            read_run_fn=methods.read_run,
        )

        await session.refresh(source)
        control = await session.get(
            SolveRun,
            result["data"]["control_run"].id,
        )

    assert control is not None
    assert source.hints_json["evaluation_report"] == evaluation_report
    assert "evaluation_report" not in control.hints_json
    assert control.hints_json["task_setting"] == "copy-me"


@pytest.mark.asyncio
async def test_control_copies_conversation_summary_and_user_inputs(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    methods = _FakeRunMethods()
    user_inputs = [
        {
            "id": "input-1",
            "revision": 3,
            "content": "use the source evidence",
        }
    ]

    async with session_factory() as session:
        source = await _create_source(session)
        source.conversation_summary = "source conversation summary"
        source.hints_json = {
            **source.hints_json,
            "user_inputs": user_inputs,
        }
        await session.commit()

        result = await create_control_experiment(
            session,
            source,
            RunControlExperimentCreate(
                factor="knowledge_mode",
                counterfactual_value="on",
                start=False,
            ),
            create_run_fn=methods.create_run,
            start_run_fn=methods.start_run,
            read_run_fn=methods.read_run,
        )

        await session.refresh(source)
        control = await session.get(
            SolveRun,
            result["data"]["control_run"].id,
        )

    assert control is not None
    assert source.conversation_summary == "source conversation summary"
    assert source.hints_json["user_inputs"] == user_inputs
    assert control.conversation_summary == source.conversation_summary
    assert control.hints_json["user_inputs"] == user_inputs


@pytest.mark.asyncio
async def test_unstarted_source_is_rejected_without_create_or_start(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    methods = _FakeRunMethods()

    async with session_factory() as session:
        source = await _create_source(session, status="CREATED")
        source_id = source.id
        challenge_id = source.challenge_id

        with pytest.raises(DomainError) as error:
            await create_control_experiment(
                session,
                source,
                RunControlExperimentCreate(
                    factor="knowledge_mode",
                    counterfactual_value="on",
                    start=True,
                ),
                create_run_fn=methods.create_run,
                start_run_fn=methods.start_run,
                read_run_fn=methods.read_run,
            )

    assert error.value.code == "SOURCE_RUN_NOT_CONTROL_ELIGIBLE"
    assert error.value.status_code == 409
    assert methods.create_calls == 0
    assert methods.started_run_ids == []

    async with session_factory() as session:
        runs = list(
            (
                await session.scalars(
                    select(SolveRun).where(
                        SolveRun.challenge_id == challenge_id
                    )
                )
            ).all()
        )

    assert [run.id for run in runs] == [source_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["RUNNING", "EXECUTING", "REPORTING"])
async def test_active_execution_source_can_create_control(
    session_factory: async_sessionmaker[AsyncSession],
    status: str,
) -> None:
    methods = _FakeRunMethods()

    async with session_factory() as session:
        source = await _create_source(session, status=status)

        result = await create_control_experiment(
            session,
            source,
            RunControlExperimentCreate(
                factor="knowledge_mode",
                counterfactual_value="on",
                start=True,
            ),
            create_run_fn=methods.create_run,
            start_run_fn=methods.start_run,
            read_run_fn=methods.read_run,
        )

    control = result["data"]["control_run"]
    assert source.status == status
    assert control.hints_json["knowledge_mode"] == "on"
    assert control.control_source_run_id == source.id
    assert result["data"]["started_run_ids"] == [control.id]
    assert methods.create_calls == 1
    assert methods.started_run_ids == [control.id]


@pytest.mark.asyncio
async def test_reserved_control_source_is_rejected_without_metadata(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    methods = _FakeRunMethods()

    async with session_factory() as session:
        source = await _create_source(session)
        reserved = SolveRun(
            challenge_id=source.challenge_id,
            control_source_run_id=source.id,
            engine_type="openai_compatible",
            solver_mode="muteki",
            hints_json={"task_setting": "control-without-audit-metadata"},
            status="COMPLETED_SOLVED",
            workspace_path="reserved-control-workspace",
        )
        session.add(reserved)
        await session.commit()

        with pytest.raises(DomainError) as error:
            await create_control_experiment(
                session,
                source,
                RunControlExperimentCreate(
                    factor="knowledge_mode",
                    counterfactual_value="on",
                    start=False,
                ),
                create_run_fn=methods.create_run,
                start_run_fn=methods.start_run,
                read_run_fn=methods.read_run,
            )

    assert error.value.code == "RUN_ALREADY_IN_CONTROL_EXPERIMENT"
    assert error.value.status_code == 409
    assert error.value.details["paired_run_id"] == reserved.id
    assert methods.create_calls == 0


@pytest.mark.asyncio
async def test_control_copies_source_skill_snapshot_not_current_skill(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    methods = _FakeRunMethods()
    source_values = {
        "skill_name": "original-snapshot",
        "skill_version": 4,
        "content_snapshot": "original skill content",
        "allowed_tools_snapshot": ["http_request", "python"],
        "config_snapshot": {"mode": "snapshot", "threshold": 7},
        "priority": 3,
    }

    async with session_factory() as session:
        source = await _create_source(session)
        skill = Skill(
            name="mutable-skill",
            display_name="Mutable Skill",
            content_markdown="current skill content",
            allowed_tools=["http_request"],
            version=9,
        )
        session.add(skill)
        await session.flush()
        session.add(
            RunSkillSnapshot(
                run_id=source.id,
                skill_id=skill.id,
                **source_values,
            )
        )
        await session.commit()

        skill.content_markdown = "changed after source snapshot"
        skill.allowed_tools = ["python", "shell"]
        skill.version = 10
        await session.commit()

        result = await create_control_experiment(
            session,
            source,
            RunControlExperimentCreate(
                factor="knowledge_mode",
                counterfactual_value="on",
                start=False,
            ),
            create_run_fn=methods.create_run,
            start_run_fn=methods.start_run,
            read_run_fn=methods.read_run,
        )

        control_id = result["data"]["control_run"].id
        control_snapshots = list(
            (
                await session.scalars(
                    select(RunSkillSnapshot)
                    .where(RunSkillSnapshot.run_id == control_id)
                    .order_by(
                        RunSkillSnapshot.priority,
                        RunSkillSnapshot.created_at,
                    )
                )
            ).all()
        )

    assert control_snapshots
    assert len(control_snapshots) == 1
    copied = control_snapshots[0]
    assert copied.skill_id == skill.id
    assert copied.skill_name == source_values["skill_name"]
    assert copied.skill_version == source_values["skill_version"]
    assert copied.content_snapshot == source_values["content_snapshot"]
    assert copied.allowed_tools_snapshot == source_values["allowed_tools_snapshot"]
    assert copied.config_snapshot == source_values["config_snapshot"]
    assert copied.priority == source_values["priority"]
    assert copied.content_snapshot != skill.content_markdown
    assert copied.skill_version != skill.version
