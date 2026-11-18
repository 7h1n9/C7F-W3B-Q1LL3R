from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.v1 import runs as runs_api
from app.core.exceptions import DomainError
from app.models.base import Base
from app.models.challenge import Challenge
from app.models.run import SolveRun
from app.models.skill import RunSkillSnapshot, Skill
from app.schemas.run import RunControlExperimentCreate
from app.services.run_control_experiment import create_control_experiment


async def _database():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _create_source(
    session,
    *,
    knowledge_mode="off",
    status="COMPLETED_SOLVED",
) -> tuple[Challenge, SolveRun]:
    challenge = Challenge(
        name="control experiment",
        target_url="http://target.test",
        allowed_hosts=["target.test"],
        challenge_type="WEB_TARGET",
        # Pairing requires an evaluator-backed challenge: without this binding
        # the two arms could never be scored (see CAP-01).
        metadata_json={"benchmark_case_id": "sql-injection-boolean"},
    )
    session.add(challenge)
    await session.flush()
    hints = {
        "task_setting": "copy-me",
        "muteki_runtime": {
            "reason_model_config_id": "reason-config",
            "worker_engines": [
                {
                    "engine_type": "openai_compatible",
                    "model_config_id": "worker-config",
                }
            ],
        },
    }
    if knowledge_mode is not None:
        hints["knowledge_mode"] = knowledge_mode
    run = SolveRun(
        challenge_id=challenge.id,
        engine_type="openai_compatible",
        solver_mode="muteki",
        model_config_id="worker-config",
        role_name="web",
        role_version="7",
        role_snapshot_json={"profile": "web", "version": 7},
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
    session.add(run)
    await session.commit()
    return challenge, run


def _install_run_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    start_error: Exception | None = None,
) -> tuple[list, list[str]]:
    captured_payloads: list = []
    started_run_ids: list[str] = []

    async def fake_create_run(challenge_id, payload, session):
        captured_payloads.append(payload)
        control = SolveRun(
            challenge_id=challenge_id,
            engine_type=payload.engine_type,
            solver_mode=payload.solver_mode,
            model_config_id=payload.model_config_id,
            role_snapshot_json={"placeholder": True},
            hints_json={"knowledge_mode": "placeholder"},
            workspace_path="control-workspace",
            max_agent_steps=payload.max_agent_steps,
            max_tool_calls=payload.max_tool_calls,
            max_context_observations=payload.max_context_observations,
            max_runtime_seconds=payload.max_runtime_seconds,
            max_total_runtime_seconds=payload.max_total_runtime_seconds,
        )
        session.add(control)
        await session.commit()
        return {"data": SimpleNamespace(id=control.id)}

    async def fake_start_run(run_id, _session):
        started_run_ids.append(run_id)
        if start_error is not None:
            raise start_error
        return {"data": {"run_id": run_id, "status": "STARTING"}}

    monkeypatch.setattr(runs_api, "create_run", fake_create_run)
    monkeypatch.setattr(runs_api, "start_run", fake_start_run)
    return captured_payloads, started_run_ids


@pytest.mark.asyncio
async def test_off_to_on_copies_configuration_and_writes_symmetric_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as session:
            _, source = await _create_source(session)
            skill = Skill(
                name="source-skill",
                display_name="Source Skill",
                content_markdown="source",
            )
            session.add(skill)
            await session.flush()
            session.add(
                RunSkillSnapshot(
                    run_id=source.id,
                    skill_id=skill.id,
                    skill_name=skill.name,
                    skill_version=1,
                    content_snapshot="source",
                    priority=4,
                )
            )
            await session.commit()
            captured, started = _install_run_fakes(monkeypatch)

            result = await runs_api.create_control_run(
                source.id,
                RunControlExperimentCreate(
                    factor="knowledge_mode",
                    counterfactual_value="on",
                    start=False,
                ),
                session,
            )

            assert result["data"]["started_run_ids"] == []
            assert result["data"]["start_error"] is None
            assert started == []
            assert len(captured) == 1
            request = captured[0]
            assert request.solver_mode == source.solver_mode
            assert request.model_config_id == source.model_config_id
            assert request.max_agent_steps == source.max_agent_steps
            assert request.max_tool_calls == source.max_tool_calls
            assert request.max_context_observations == source.max_context_observations
            assert request.max_runtime_seconds == source.max_runtime_seconds
            assert request.max_total_runtime_seconds == source.max_total_runtime_seconds
            assert request.selected_skill_ids == []

            control_id = result["data"]["control_run"].id
            await session.refresh(source)
            control = await session.get(SolveRun, control_id)
            assert control is not None
            assert control.role_name == source.role_name
            assert control.role_version == source.role_version
            assert control.role_snapshot_json == source.role_snapshot_json
            assert control.hints_json["task_setting"] == "copy-me"
            assert control.hints_json["knowledge_mode"] == "on"
            assert source.hints_json["knowledge_mode"] == "off"
            source_snapshots = list(
                (
                    await session.scalars(
                        select(RunSkillSnapshot)
                        .where(RunSkillSnapshot.run_id == source.id)
                        .order_by(
                            RunSkillSnapshot.priority,
                            RunSkillSnapshot.created_at,
                        )
                    )
                ).all()
            )
            control_snapshots = list(
                (
                    await session.scalars(
                        select(RunSkillSnapshot)
                        .where(RunSkillSnapshot.run_id == control.id)
                        .order_by(
                            RunSkillSnapshot.priority,
                            RunSkillSnapshot.created_at,
                        )
                    )
                ).all()
            )
            assert [
                (
                    snapshot.skill_id,
                    snapshot.skill_name,
                    snapshot.skill_version,
                    snapshot.content_snapshot,
                    snapshot.allowed_tools_snapshot,
                    snapshot.config_snapshot,
                    snapshot.priority,
                )
                for snapshot in control_snapshots
            ] == [
                (
                    snapshot.skill_id,
                    snapshot.skill_name,
                    snapshot.skill_version,
                    snapshot.content_snapshot,
                    snapshot.allowed_tools_snapshot,
                    snapshot.config_snapshot,
                    snapshot.priority,
                )
                for snapshot in source_snapshots
            ]

            source_meta = source.hints_json["ab_experiment"]
            control_meta = control.hints_json["ab_experiment"]
            assert source_meta == {
                "schema_version": 1,
                "experiment_id": control_meta["experiment_id"],
                "factor": "knowledge_mode",
                "role": "baseline",
                "baseline_value": "off",
                "treatment_value": "on",
                "source_run_id": source.id,
                "paired_run_id": control.id,
            }
            assert control_meta == {
                **source_meta,
                "role": "treatment",
                "paired_run_id": source.id,
            }
            assert source.status == "COMPLETED_SOLVED"
            assert "success" not in source_meta
            assert "evaluation_report" not in source_meta
            assert "success" not in control_meta
            assert "evaluation_report" not in control_meta
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_on_to_off_makes_new_run_the_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as session:
            _, source = await _create_source(session, knowledge_mode="on")
            _install_run_fakes(monkeypatch)

            result = await runs_api.create_control_run(
                source.id,
                RunControlExperimentCreate(
                    factor="knowledge_mode",
                    counterfactual_value="off",
                ),
                session,
            )

            control = await session.get(
                SolveRun,
                result["data"]["control_run"].id,
            )
            assert control is not None
            assert source.hints_json["ab_experiment"]["role"] == "treatment"
            assert control.hints_json["ab_experiment"]["role"] == "baseline"
            assert control.hints_json["knowledge_mode"] == "off"
    finally:
        await engine.dispose()


def test_request_schema_rejects_invalid_factor_and_value() -> None:
    with pytest.raises(ValidationError):
        RunControlExperimentCreate(
            factor="model",
            counterfactual_value="on",
        )
    with pytest.raises(ValidationError):
        RunControlExperimentCreate(
            factor="knowledge_mode",
            counterfactual_value="maybe",
        )


@pytest.mark.asyncio
async def test_service_rejects_invalid_factor_and_value() -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as session:
            _, source = await _create_source(session)

            async def unused_create_run(*_args):
                raise AssertionError("create_run must not be called")

            async def unused_start_run(*_args):
                raise AssertionError("start_run must not be called")

            invalid_factor = RunControlExperimentCreate.model_construct(
                factor="model",
                counterfactual_value="on",
                start=False,
            )
            with pytest.raises(DomainError) as factor_error:
                await create_control_experiment(
                    session,
                    source,
                    invalid_factor,
                    create_run_fn=unused_create_run,
                    start_run_fn=unused_start_run,
                    read_run_fn=lambda run: run,
                )
            assert (
                factor_error.value.code
                == "CONTROL_EXPERIMENT_FACTOR_UNSUPPORTED"
            )

            invalid_value = RunControlExperimentCreate.model_construct(
                factor="knowledge_mode",
                counterfactual_value="maybe",
                start=False,
            )
            with pytest.raises(DomainError) as value_error:
                await create_control_experiment(
                    session,
                    source,
                    invalid_value,
                    create_run_fn=unused_create_run,
                    start_run_fn=unused_start_run,
                    read_run_fn=lambda run: run,
                )
            assert value_error.value.code == "CONTROL_EXPERIMENT_VALUE_INVALID"

            same_value = RunControlExperimentCreate(
                factor="knowledge_mode",
                counterfactual_value="off",
                start=False,
            )
            with pytest.raises(DomainError) as mismatch_error:
                await create_control_experiment(
                    session,
                    source,
                    same_value,
                    create_run_fn=unused_create_run,
                    start_run_fn=unused_start_run,
                    read_run_fn=lambda run: run,
                )
            assert (
                mismatch_error.value.code
                == "CONTROL_EXPERIMENT_VALUE_MISMATCH"
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_existing_source_pairing_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as session:
            _, source = await _create_source(session)
            source.hints_json = {
                **source.hints_json,
                "ab_experiment": {
                    "schema_version": 1,
                    "experiment_id": "existing-experiment",
                    "factor": "knowledge_mode",
                    "paired_run_id": "existing-control",
                },
            }
            await session.commit()
            captured, started = _install_run_fakes(monkeypatch)

            with pytest.raises(DomainError) as error:
                await runs_api.create_control_run(
                    source.id,
                    RunControlExperimentCreate(
                        factor="knowledge_mode",
                        counterfactual_value="on",
                    ),
                    session,
                )

            assert error.value.code == "RUN_ALREADY_IN_CONTROL_EXPERIMENT"
            assert captured == []
            assert started == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cycle_or_duplicate_pair_reference_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as session:
            challenge, source = await _create_source(session)
            orphan = SolveRun(
                challenge_id=challenge.id,
                workspace_path="orphan-workspace",
                hints_json={
                    "knowledge_mode": "on",
                    "ab_experiment": {
                        "schema_version": 1,
                        "experiment_id": "cycle-experiment",
                        "factor": "knowledge_mode",
                        "paired_run_id": source.id,
                    },
                },
            )
            session.add(orphan)
            await session.commit()
            captured, started = _install_run_fakes(monkeypatch)

            with pytest.raises(DomainError) as error:
                await runs_api.create_control_run(
                    source.id,
                    RunControlExperimentCreate(
                        factor="knowledge_mode",
                        counterfactual_value="on",
                    ),
                    session,
                )

            assert error.value.code == "RUN_ALREADY_IN_CONTROL_EXPERIMENT"
            assert captured == []
            assert started == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_start_true_starts_only_new_control_and_duplicate_does_not_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as session:
            _, source = await _create_source(session)
            captured, started = _install_run_fakes(monkeypatch)
            payload = RunControlExperimentCreate(
                factor="knowledge_mode",
                counterfactual_value="on",
                start=True,
            )

            result = await runs_api.create_control_run(
                source.id,
                payload,
                session,
            )

            control_id = result["data"]["control_run"].id
            assert result["data"]["started_run_ids"] == [control_id]
            assert started == [control_id]
            assert started != [source.id]

            with pytest.raises(DomainError):
                await runs_api.create_control_run(
                    source.id,
                    payload,
                    session,
                )
            assert started == [control_id]
            assert len(captured) == 1
            run_ids = list((await session.scalars(select(SolveRun.id))).all())
            assert len(run_ids) == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_start_failure_returns_created_control_and_explicit_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as session:
            _, source = await _create_source(session)
            _install_run_fakes(
                monkeypatch,
                start_error=DomainError(
                    "RUN_INVALID_STATE",
                    "Control Run cannot start.",
                    {"current_state": "CREATED"},
                    status_code=409,
                ),
            )

            result = await runs_api.create_control_run(
                source.id,
                RunControlExperimentCreate(
                    factor="knowledge_mode",
                    counterfactual_value="on",
                    start=True,
                ),
                session,
            )

            assert result["data"]["started_run_ids"] == []
            assert result["data"]["start_error"] == {
                "code": "RUN_INVALID_STATE",
                "message": "Control Run cannot start.",
                "details": {"current_state": "CREATED"},
            }
            control = await session.get(
                SolveRun,
                result["data"]["control_run"].id,
            )
            assert control is not None
            assert control.hints_json["knowledge_mode"] == "on"
            assert "ab_experiment" in source.hints_json
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_missing_source_knowledge_mode_uses_runtime_default_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as session:
            _, source = await _create_source(session, knowledge_mode=None)
            _install_run_fakes(monkeypatch)

            result = await runs_api.create_control_run(
                source.id,
                RunControlExperimentCreate(
                    factor="knowledge_mode",
                    counterfactual_value="on",
                ),
                session,
            )

            control = await session.get(
                SolveRun,
                result["data"]["control_run"].id,
            )
            assert control is not None
            assert source.hints_json["knowledge_mode"] == "off"
            assert control.hints_json["knowledge_mode"] == "on"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pairing_refuses_a_challenge_without_an_evaluator_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CAP-01: an unscorable challenge must never produce a paired experiment.

    Experiment 8ddbdb34 burned a full pair of Runs and was only discovered to be
    unusable during review, because the challenge carried no
    ``benchmark_case_id``.  The pairing endpoint has to refuse that up front
    instead of quietly emitting ``descriptive_only``.
    """

    engine, sessions = await _database()
    try:
        async with sessions() as session:
            challenge, source = await _create_source(session)
            challenge.metadata_json = {}
            await session.commit()
            _install_run_fakes(monkeypatch)

            with pytest.raises(DomainError) as error:
                await runs_api.create_control_run(
                    source.id,
                    RunControlExperimentCreate(
                        factor="knowledge_mode",
                        counterfactual_value="on",
                    ),
                    session,
                )

            assert error.value.code == "SOURCE_CHALLENGE_NOT_EVALUATABLE"
            assert error.value.status_code == 409
            assert error.value.details["benchmark_case_id"] is None
            # Nothing was paired, so the source is still free to be paired later.
            assert (
                await session.scalar(
                    select(SolveRun.id).where(
                        SolveRun.control_source_run_id == source.id
                    )
                )
            ) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pairing_accepts_a_challenge_with_an_evaluator_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions = await _database()
    try:
        async with sessions() as session:
            _, source = await _create_source(session)
            _install_run_fakes(monkeypatch)

            result = await runs_api.create_control_run(
                source.id,
                RunControlExperimentCreate(
                    factor="knowledge_mode",
                    counterfactual_value="on",
                ),
                session,
            )

            assert result["data"]["control_run"].id
    finally:
        await engine.dispose()
