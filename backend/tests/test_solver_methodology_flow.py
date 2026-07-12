from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.challenge import Challenge
from app.models.run import Artifact, FlagCandidate, Observation, RunEvent, SolveRun, ToolCall
from app.models.skill import RunSkillSnapshot, Skill
from app.services.action_fingerprint import fingerprint_action
from app.services.builtin_skills import BuiltinSkillSyncService
from app.services.codex_materializer import codex_materializer
from app.services.finish_gate import finish_gate
from app.services.flags import flag_service
from app.services.hypotheses import hypothesis_service
from app.schemas.agent import SkillAction
from app.services.role_loader import role_loader
from app.services.run_diagnostics import run_diagnostics_service
from app.services.solver_state import solver_state_service
from app.orchestration.state_machine import RunStatus, transition


def test_role_loader_selects_role_by_challenge_type() -> None:
    web_role = role_loader.load("WEB_TARGET")
    traffic_role = role_loader.load("TRAFFIC_ANALYSIS")

    assert web_role.name == "web-ctf-solver"
    assert "http_request" in web_role.tools
    assert traffic_role.name == "traffic-ctf-solver"
    assert "pcap_metadata" in traffic_role.tools


@pytest.mark.asyncio
async def test_builtin_skill_sync_infers_methodology_metadata(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    core = root / "ctf-solver-core"
    web = root / "web-ctf-methodology"
    core.mkdir(parents=True)
    web.mkdir(parents=True)
    (core / "SKILL.md").write_text(
        "---\nname: ctf-solver-core\ndisplay_name: CTF Solver Core\ndescription: core\n---\nCore body",
        encoding="utf-8",
    )
    (web / "SKILL.md").write_text(
        "---\nname: web-ctf-methodology\ndisplay_name: Web Methodology\ndescription: web\nchallenge_types: [WEB_TARGET]\n---\nWeb body",
        encoding="utf-8",
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        service = BuiltinSkillSyncService(root=root)
        await service.sync(session)
        skills = {item.name: item for item in (await session.scalars(select(Skill))).all()}

    assert skills["ctf-solver-core"].skill_kind == "CORE"
    assert skills["ctf-solver-core"].activation_mode == "ALWAYS"
    assert skills["web-ctf-methodology"].skill_kind == "METHODOLOGY"
    assert skills["web-ctf-methodology"].activation_mode == "AUTO"
    await engine.dispose()


@pytest.mark.asyncio
async def test_solver_state_initialization_and_finish_gate(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'solver.db'}", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        challenge = Challenge(
            name="demo",
            target_url="http://demo.local",
            allowed_hosts=["demo.local"],
            challenge_type="WEB_TARGET",
            flag_pattern=r"flag\{[^}]+\}",
        )
        session.add(challenge)
        await session.flush()
        run = SolveRun(challenge_id=challenge.id, workspace_path=str(tmp_path), role_snapshot_json={})
        session.add(run)
        await session.flush()
        state = await solver_state_service.initialize(session, run, challenge.challenge_type, [])
        assert state.current_phase == "INTAKE"
        state.confirmed_facts_json = [{"source": "http_request"}]
        state.rejected_paths_json = [{"source": "tool", "reason": "blocked"}]
        state.active_hypotheses_json = [{"id": "h1", "statement": "Try login", "confidence": 40}]
        await session.commit()
        ok, code, message = await finish_gate.evaluate(session, run, challenge)
        assert ok, message
        assert code == "OK"
    await engine.dispose()


@pytest.mark.asyncio
async def test_hypothesis_service_creates_runtime_hypothesis(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'hypothesis.db'}", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        challenge = Challenge(
            name="demo",
            target_url="http://demo.local",
            allowed_hosts=["demo.local"],
            challenge_type="WEB_TARGET",
            flag_pattern=r"flag\{[^}]+\}",
        )
        session.add(challenge)
        await session.flush()
        run = SolveRun(challenge_id=challenge.id, workspace_path=str(tmp_path), role_snapshot_json={})
        session.add(run)
        await session.flush()
        created, was_new = await hypothesis_service.upsert_from_action(
            session,
            run.id,
            phase="TESTING",
            objective="Check auth flow",
            hypothesis_text="Session cookie is weak",
            evidence={"tool_name": "http_request"},
        )
        await solver_state_service.initialize(session, run, challenge.challenge_type, [])
        active = await solver_state_service.sync_hypotheses(session, run.id)

    assert was_new is True
    assert created.title == "Session cookie is weak"
    assert active and active[0]["statement"] == "Session cookie is weak"
    await engine.dispose()


def test_action_fingerprint_is_stable() -> None:
    first = fingerprint_action("http_request", {"b": 2, "a": 1})
    second = fingerprint_action("http_request", {"a": 1, "b": 2})
    assert first == second


def test_timeout_transition_is_allowed_from_active_states() -> None:
    run = SolveRun(challenge_id="demo", workspace_path=".", role_snapshot_json={}, status="PLANNING", current_phase="PLANNING")
    transition(run, RunStatus.TIMEOUT)
    assert run.status == RunStatus.TIMEOUT.value


@pytest.mark.asyncio
async def test_codex_materializer_backfills_workspace_details(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'codex.db'}", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        challenge = Challenge(
            name="demo",
            target_url="http://demo.local",
            allowed_hosts=["demo.local"],
            challenge_type="WEB_TARGET",
            flag_pattern=r"flag\{[^}]+\}",
        )
        session.add(challenge)
        await session.flush()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        evidence = workspace / "evidence" / "demo.md"
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text("flag{demo}", encoding="utf-8")
        run = SolveRun(
            challenge_id=challenge.id,
            workspace_path=str(workspace),
            role_snapshot_json={},
            engine_type="codex_sdk",
            status="COMPLETED_UNSOLVED",
            current_phase="COMPLETED_UNSOLVED",
            event_sequence=4,
        )
        session.add(run)
        await session.flush()
        session.add_all(
            [
                RunEvent(run_id=run.id, sequence=1, event_type="agent.message", payload_json={"item_id": "item_1", "message": "step"}),
                RunEvent(
                    run_id=run.id,
                    sequence=2,
                    event_type="tool.started",
                    payload_json={"tool_call_id": "item_1", "tool": "command_execution", "command": "echo demo"},
                ),
                RunEvent(
                    run_id=run.id,
                    sequence=3,
                    event_type="tool.completed",
                    payload_json={"tool_call_id": "item_1", "tool": "command_execution", "command": "echo demo", "output": "flag{demo}", "exit_code": 0},
                ),
                RunEvent(
                    run_id=run.id,
                    sequence=4,
                    event_type="artifact.created",
                    payload_json={"changes": [{"kind": "add", "path": str(evidence)}]},
                ),
            ]
        )
        await session.commit()
        await codex_materializer.sync(session, run)
        report_path = workspace / "final" / "writeup.md"

        tools = (await session.scalars(select(ToolCall).where(ToolCall.run_id == run.id))).all()
        observations = (await session.scalars(select(Observation).where(Observation.run_id == run.id))).all()
        artifacts_rows = (await session.scalars(select(Artifact).where(Artifact.run_id == run.id))).all()
        flags = (await session.scalars(select(FlagCandidate).where(FlagCandidate.run_id == run.id))).all()

    assert tools and tools[0].runner_job_id == "codex:item_1"
    assert observations and observations[0].summary
    assert artifacts_rows and any(item.file_path.endswith("responses/codex_sdk/codex_item_1.txt") for item in artifacts_rows)
    assert flags and flags[0].candidate == "flag{demo}"
    assert report_path.is_file()
    await engine.dispose()


@pytest.mark.asyncio
async def test_manual_flag_review_controls_finish_gate(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'flag.db'}", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        challenge = Challenge(
            name="demo",
            target_url="http://demo.local",
            allowed_hosts=["demo.local"],
            challenge_type="WEB_TARGET",
            flag_pattern=r"flag\{[^}]+\}",
        )
        session.add(challenge)
        await session.flush()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        run = SolveRun(challenge_id=challenge.id, workspace_path=str(workspace), role_snapshot_json={})
        session.add(run)
        await session.flush()
        await solver_state_service.initialize(session, run, challenge.challenge_type, [])
        state = await solver_state_service.load(session, run.id)
        assert state is not None
        state.confirmed_facts_json = [{"source": "http_request"}]
        state.rejected_paths_json = [{"source": "tool", "reason": "blocked"}]
        state.active_hypotheses_json = [{"id": "h1", "statement": "Check admin", "confidence": 30}]
        candidate = FlagCandidate(run_id=run.id, candidate="flag{demo}", review_state="OPEN")
        session.add(candidate)
        await session.commit()

        blocked, _, _ = await finish_gate.evaluate(session, run, challenge)
        assert blocked is False

        run.status = "FAILED_ENGINE"
        run.current_phase = "FAILED_ENGINE"
        await session.commit()

        reviewed = await flag_service.set_review_state(session, run, candidate.id, "INVALID")
        assert reviewed.review_state == "INVALID"
        assert challenge.status == "ACTIVE"
        assert run.status == "FAILED_ENGINE"

        reviewed = await flag_service.set_review_state(session, run, candidate.id, "VALID")
        assert reviewed.review_state == "VALID"
        assert challenge.status == "SOLVED"
        assert run.status == "COMPLETED_SOLVED"
        assert run.current_phase == "COMPLETED_SOLVED"
    await engine.dispose()


@pytest.mark.asyncio
async def test_valid_flag_reconciles_failed_run_to_solved(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'reconcile.db'}", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        challenge = Challenge(
            name="demo",
            target_url="http://demo.local",
            allowed_hosts=["demo.local"],
            challenge_type="WEB_TARGET",
            flag_pattern=r"flag\{[^}]+\}",
        )
        session.add(challenge)
        await session.flush()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        run = SolveRun(
            challenge_id=challenge.id,
            workspace_path=str(workspace),
            role_snapshot_json={},
            status="FAILED_ENGINE",
            current_phase="FAILED_ENGINE",
        )
        session.add(run)
        await session.flush()
        candidate = FlagCandidate(
            run_id=run.id,
            candidate="flag{demo}",
            verified=True,
            review_state="VALID",
        )
        session.add(candidate)
        await session.commit()

        changed = await flag_service.reconcile_run_status(session, run)
        assert changed is True
        assert run.status == "COMPLETED_SOLVED"
        assert run.current_phase == "COMPLETED_SOLVED"
        assert challenge.status == "SOLVED"
    await engine.dispose()


@pytest.mark.asyncio
async def test_skill_action_activation_creates_snapshot(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'skill_action.db'}", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        challenge = Challenge(
            name="demo",
            target_url="http://demo.local",
            allowed_hosts=["demo.local"],
            challenge_type="WEB_TARGET",
            flag_pattern=r"flag\{[^}]+\}",
        )
        skill = Skill(
            name="web-login-method",
            display_name="Web Login Method",
            description="demo",
            source_type="CUSTOM",
            skill_kind="SPECIALIST",
            activation_mode="MANUAL",
            triggers=["/login"],
            prerequisites=[],
            required_tools=[],
            recommended_tools=["http_request"],
            forbidden_tools=[],
            ctf_phases=["PLANNING"],
            challenge_types=["WEB_TARGET"],
            content_markdown="Use a login-aware approach.",
            allowed_tools=["http_request"],
            risk_level="low",
            version=1,
            enabled=True,
        )
        session.add_all([challenge, skill])
        await session.flush()
        run = SolveRun(challenge_id=challenge.id, workspace_path=str(tmp_path), role_snapshot_json={})
        session.add(run)
        await session.flush()
        run.status = "PLANNING"
        run.current_phase = "PLANNING"
        await solver_state_service.initialize(session, run, challenge.challenge_type, [])
        from app.orchestration.orchestrator import SolveOrchestrator

        handled = await SolveOrchestrator()._handle_skill_action(
            session,
            run,
            challenge,
            SkillAction(
                type="skill",
                operation="activate",
                phase="PLANNING",
                objective="Enable login methodology",
                reason="Need a login-aware path",
                skill_id=skill.id,
                skill_name=skill.name,
                supporting_evidence=["/login"],
                expected_use="Inspect authentication flow",
            ),
        )
        state = await solver_state_service.load(session, run.id)
        snapshot = await session.scalar(
            select(RunSkillSnapshot).where(
                RunSkillSnapshot.run_id == run.id, RunSkillSnapshot.skill_id == skill.id
            )
        )

    assert handled is True
    assert state is not None and skill.id in (state.active_skill_ids_json or [])
    assert snapshot is not None and snapshot.skill_name == skill.name
    await engine.dispose()


@pytest.mark.asyncio
async def test_run_diagnostics_flags_contract_and_redirect_loops(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'diagnostics.db'}", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        challenge = Challenge(
            name="demo",
            target_url="http://demo.local",
            allowed_hosts=["demo.local"],
            challenge_type="WEB_TARGET",
            flag_pattern=r"flag\{[^}]+\}",
        )
        session.add(challenge)
        await session.flush()
        run = SolveRun(
            challenge_id=challenge.id,
            workspace_path=str(tmp_path),
            role_snapshot_json={},
            status="FAILED_ENGINE",
            current_phase="FAILED_ENGINE",
            last_error_message="400: python_run only accepts existing scripts/*.py files",
            last_error_code="ENGINE_ERROR",
        )
        session.add(run)
        await session.flush()
        session.add_all(
            [
                ToolCall(
                    run_id=run.id,
                    tool_name="http_request",
                    arguments_json={"url": "http://demo.local/profile"},
                    status="COMPLETED",
                ),
                Observation(
                    run_id=run.id,
                    observation_type="tool_result",
                    summary="GET /profile -> 302 /login",
                    facts_json={"path": "/profile", "status_code": 302},
                ),
                Observation(
                    run_id=run.id,
                    observation_type="tool_result",
                    summary="GET /admin -> 302 /login",
                    facts_json={"path": "/admin", "status_code": 302},
                ),
            ]
        )
        await session.commit()
        diagnostics = await run_diagnostics_service.analyze(session, run)

    assert "TOOL_CONTRACT_MISMATCH" in diagnostics["diagnostic_tags"]
    assert "AUTH_REDIRECT_LOOP" in diagnostics["diagnostic_tags"]
    assert any(item["code"] == "TOOL_CONTRACT_MISMATCH" for item in diagnostics["anomalies"])
    assert any(item["code"] == "AUTH_REDIRECT_LOOP" for item in diagnostics["anomalies"])
    await engine.dispose()
