"""Focused DB-backed tests for knowledge experiment pair aggregation."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.v1.knowledge import router
from app.benchmark.evaluation import EVALUATOR_NAME, EVALUATOR_SCHEMA_VERSION
from app.core.database import get_session
from app.core.exceptions import DomainError, domain_error_handler
from app.models import Base, Challenge, SolveRun

VALID_EXPERIMENT_ID = "11111111-1111-4111-8111-111111111111"
VALID_RUN_IDS = (
    "22222222-2222-4222-8222-222222222222",
    "33333333-3333-4333-8333-333333333333",
)


@pytest.fixture
async def api_client(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]]]:
    monkeypatch.setenv(
        "C7F_KNOWLEDGE_EXPERIMENT_ROOT",
        str(tmp_path / "missing-file-experiments"),
    )
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app = FastAPI()
    app.add_exception_handler(DomainError, domain_error_handler)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_session] = override_get_session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        yield client, session_factory

    app.dependency_overrides.clear()
    await engine.dispose()


async def _create_challenge(
    session: AsyncSession,
    label: str,
    benchmark_case_id: str | None = None,
) -> str:
    challenge = Challenge(
        name=f"Knowledge DB pair {label}",
        description="Focused knowledge experiment aggregation fixture.",
        challenge_type="WEB_TARGET",
        target_url="http://target.test/search",
        allowed_hosts=["target.test"],
        metadata_json=(
            {"benchmark_case_id": benchmark_case_id}
            if benchmark_case_id is not None
            else {}
        ),
    )
    session.add(challenge)
    await session.flush()
    return challenge.id


async def _create_pair(
    session: AsyncSession,
    *,
    experiment_id: str,
    run_ids: tuple[str, str],
    challenge_ids: tuple[str, str] | None = None,
    roles: tuple[str, str] = ("baseline", "treatment"),
    modes: tuple[str, str] = ("off", "on"),
    source_run_ids: tuple[str, str] | None = None,
    paired_run_ids: tuple[str, str] | None = None,
    statuses: tuple[str, str] = ("COMPLETED_SOLVED", "COMPLETED_SOLVED"),
    final_results: tuple[str | None, str | None] = ("SUCCESS", "SUCCESS"),
    arm_values: tuple[str | None, str | None] = (None, None),
) -> None:
    if challenge_ids is None:
        benchmark_case_id = f"case-{experiment_id}"
        challenge_id = await _create_challenge(
            session,
            experiment_id,
            benchmark_case_id,
        )
        challenge_ids = (challenge_id, challenge_id)
    else:
        benchmark_case_id = f"case-{experiment_id}"
    if source_run_ids is None:
        source_run_ids = (run_ids[0], run_ids[0])
    if paired_run_ids is None:
        paired_run_ids = (run_ids[1], run_ids[0])

    for index, run_id in enumerate(run_ids):
        ab_experiment = {
            "schema_version": 1,
            "experiment_id": experiment_id,
            "factor": "knowledge_mode",
            "role": roles[index],
            "baseline_value": "off",
            "treatment_value": "on",
            "source_run_id": source_run_ids[index],
            "paired_run_id": paired_run_ids[index],
        }
        if arm_values[index] is not None:
            ab_experiment["arm"] = arm_values[index]

        hints: dict = {
            "knowledge_mode": modes[index],
            "ab_experiment": ab_experiment,
        }
        report_json: dict = {}
        if final_results[index] is not None:
            report_json["evaluation_report"] = _evaluation_report(
                run_id,
                final_results[index],
                benchmark_case_id,
            )

        session.add(
            SolveRun(
                id=run_id,
                challenge_id=challenge_ids[index],
                workspace_path=f"./{experiment_id}/{modes[index]}",
                status=statuses[index],
                hints_json=hints,
                report_json=report_json,
            )
        )

    await session.commit()


def _evaluation_report(
    run_id: str,
    final_result: object,
    case_id: str,
) -> dict:
    return {
        "case_id": case_id,
        "run_id": run_id,
        "final_result": final_result,
        "metrics": {},
        "evaluator": {
            "name": EVALUATOR_NAME,
            "schema_version": EVALUATOR_SCHEMA_VERSION,
            "run_id": run_id,
            "basis": "durable_run_evidence",
        },
    }


def _test_uuid(case: str, label: str) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"knowledge-db-pair:{case}:{label}",
        )
    )


def _experiment(payload: dict, experiment_id: str) -> dict:
    return next(
        item
        for item in payload["data"]["experiments"]
        if item["id"] == experiment_id
    )


async def test_list_exposes_symmetric_evaluator_backed_pair(
    api_client: tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, session_factory = api_client
    async with session_factory() as session:
        await _create_pair(
            session,
            experiment_id=VALID_EXPERIMENT_ID,
            run_ids=VALID_RUN_IDS,
        )

    response = await client.get("/api/v1/knowledge/experiments")

    assert response.status_code == 200
    body = response.json()
    assert "db_error" not in body["data"]
    assert body["data"]["experiments"]
    experiment = _experiment(body, VALID_EXPERIMENT_ID)
    assert experiment["source"] == "db_pair"
    assert experiment["id"] == VALID_EXPERIMENT_ID
    assert experiment["baseline_run_id"] == VALID_RUN_IDS[0]
    assert experiment["treatment_run_id"] == VALID_RUN_IDS[1]

    arms = {arm["arm"]: arm for arm in experiment["arms"]}
    assert list(arms) == ["off", "on"]
    assert arms["off"]["role"] == "baseline"
    assert arms["on"]["role"] == "treatment"
    for arm in arms.values():
        assert arm["terminal_status"] == "COMPLETED_SOLVED"
        assert arm["evaluator_backed"] is True
        assert arm["final_result"] == "SUCCESS"
        assert arm["success"] is True

    paired_effect = experiment["paired_effect"]
    assert paired_effect["sample_count"] == 1
    assert paired_effect["claim"] == "descriptive_only"
    assert paired_effect["sample_adequate"] is False
    assert paired_effect["success_delta"] == 0.0


async def test_completed_solved_without_evaluation_report_does_not_infer_success(
    api_client: tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, session_factory = api_client
    async with session_factory() as session:
        await _create_pair(
            session,
            experiment_id=VALID_EXPERIMENT_ID,
            run_ids=VALID_RUN_IDS,
            final_results=(None, None),
        )

    response = await client.get("/api/v1/knowledge/experiments")

    assert response.status_code == 200
    experiment = _experiment(response.json(), VALID_EXPERIMENT_ID)
    assert experiment["paired_effect"]["success_delta"] is None
    for arm in experiment["arms"]:
        assert arm["terminal_status"] == "COMPLETED_SOLVED"
        assert arm["evaluation_report"] is None
        assert arm["evaluator_backed"] is False
        assert arm["final_result"] is None
        assert arm["success"] is None


@pytest.mark.parametrize(
    "case",
    [
        "bare-hints",
        "nested-ab-experiment",
        "bare-report-json",
        "wrong-report-run-id",
        "wrong-evaluator-run-id",
        "invalid-final-type",
        "wrong-evaluator-name",
        "mismatched-case-id",
        "bool-evaluator-schema",
        "wrong-evaluator-schema",
    ],
)
async def test_rejects_untrusted_evaluation_reports(
    api_client: tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]],
    case: str,
) -> None:
    client, session_factory = api_client
    experiment_id = _test_uuid(case, "experiment")
    run_ids = (
        _test_uuid(case, "off-run"),
        _test_uuid(case, "on-run"),
    )
    async with session_factory() as session:
        await _create_pair(
            session,
            experiment_id=experiment_id,
            run_ids=run_ids,
            final_results=(None, None),
        )
        runs = list(
            (
                await session.scalars(
                    select(SolveRun).where(SolveRun.id.in_(run_ids))
                )
            ).all()
        )
        for run in runs:
            report = _evaluation_report(
                run.id,
                "SUCCESS",
                f"case-{experiment_id}",
            )
            hints = dict(run.hints_json or {})
            if case == "bare-hints":
                hints["evaluation_report"] = report
            elif case == "nested-ab-experiment":
                hints["ab_experiment"] = {
                    **dict(hints["ab_experiment"]),
                    "evaluation_report": report,
                }
            elif case == "bare-report-json":
                run.report_json = {
                    "evaluation_report": {"final_result": "SUCCESS"}
                }
            elif case == "wrong-report-run-id":
                report["run_id"] = "different-run"
                run.report_json = {"evaluation_report": report}
            elif case == "wrong-evaluator-run-id":
                report["evaluator"]["run_id"] = "different-run"
                run.report_json = {"evaluation_report": report}
            elif case == "invalid-final-type":
                report["final_result"] = ["SUCCESS"]
                run.report_json = {"evaluation_report": report}
            elif case == "wrong-evaluator-name":
                report["evaluator"]["name"] = "spoofed-evaluator"
                run.report_json = {"evaluation_report": report}
            elif case == "mismatched-case-id":
                report["case_id"] = "different-benchmark-case"
                run.report_json = {"evaluation_report": report}
            elif case == "bool-evaluator-schema":
                report["evaluator"]["schema_version"] = True
                run.report_json = {"evaluation_report": report}
            elif case == "wrong-evaluator-schema":
                report["evaluator"]["schema_version"] = 2
                run.report_json = {"evaluation_report": report}
            run.hints_json = hints
        await session.commit()

    response = await client.get("/api/v1/knowledge/experiments")

    assert response.status_code == 200
    experiment = _experiment(response.json(), experiment_id)
    assert experiment["paired_effect"]["success_delta"] is None
    for arm in experiment["arms"]:
        assert arm["evaluation_report"] is None
        assert arm["evaluator_backed"] is False
        assert arm["final_result"] is None
        assert arm["success"] is None


@pytest.mark.parametrize(
    "case",
    [
        "asymmetric-paired-run-id",
        "role-arm-mismatch",
        "different-challenge-id",
        "different-source-run-id",
    ],
)
async def test_rejects_invalid_pairs(
    api_client: tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]],
    case: str,
) -> None:
    client, session_factory = api_client
    experiment_id = _test_uuid(case, "experiment")
    run_ids = (
        _test_uuid(case, "off-run"),
        _test_uuid(case, "on-run"),
    )
    kwargs: dict = {}
    if case == "asymmetric-paired-run-id":
        kwargs["paired_run_ids"] = (
            run_ids[1],
            _test_uuid(case, "unrelated-run"),
        )
    elif case == "role-arm-mismatch":
        kwargs["modes"] = ("on", "off")
    elif case == "different-challenge-id":
        async with session_factory() as session:
            off_challenge_id = await _create_challenge(session, f"{case}-off")
            on_challenge_id = await _create_challenge(session, f"{case}-on")
            await session.commit()
        kwargs["challenge_ids"] = (off_challenge_id, on_challenge_id)
    elif case == "different-source-run-id":
        kwargs["source_run_ids"] = run_ids

    async with session_factory() as session:
        await _create_pair(
            session,
            experiment_id=experiment_id,
            run_ids=run_ids,
            **kwargs,
        )

    listing = await client.get("/api/v1/knowledge/experiments")
    detail = await client.get(f"/api/v1/knowledge/experiments/{experiment_id}")

    assert listing.status_code == 200
    assert all(
        item["id"] != experiment_id
        for item in listing.json()["data"]["experiments"]
    )
    assert detail.status_code == 404
    assert detail.json()["code"] == "KNOWLEDGE_EXPERIMENT_NOT_FOUND"


async def test_detail_reads_the_db_pair(
    api_client: tuple[httpx.AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, session_factory = api_client
    async with session_factory() as session:
        await _create_pair(
            session,
            experiment_id=VALID_EXPERIMENT_ID,
            run_ids=VALID_RUN_IDS,
        )

    response = await client.get(
        f"/api/v1/knowledge/experiments/{VALID_EXPERIMENT_ID}"
    )

    assert response.status_code == 200
    experiment = response.json()["data"]
    assert experiment["id"] == VALID_EXPERIMENT_ID
    assert experiment["source"] == "db_pair"
    assert experiment["minimum_paired_samples"] == 10
    assert [arm["arm"] for arm in experiment["arms"]] == ["off", "on"]
    assert [arm["run_id"] for arm in experiment["arms"]] == list(VALID_RUN_IDS)
