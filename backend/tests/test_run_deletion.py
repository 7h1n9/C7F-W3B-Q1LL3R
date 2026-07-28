from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.v1 import runs as runs_api
from app.models.base import Base
from app.models.challenge import Challenge
from app.models.run import (
    Artifact,
    FlagCandidate,
    Hypothesis,
    Observation,
    RunEvent,
    SolveRun,
    ToolCall,
)
from app.models.skill import RunSkillSnapshot, Skill
from app.schemas.run import RunBatchDelete


@pytest.mark.asyncio
async def test_delete_run_records_and_workspace(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    root = tmp_path / "workspaces"
    workspace = root / "run-1"
    workspace.mkdir(parents=True)
    (workspace / "evidence.txt").write_text("evidence", encoding="utf-8")
    monkeypatch.setattr(runs_api, "get_settings", lambda: SimpleNamespace(workspace_root=root))

    async with sessions() as session:
        challenge = Challenge(name="delete", target_url="http://test.local", allowed_hosts=["test.local"])
        skill = Skill(name="delete-skill", display_name="Delete Skill", content_markdown="test")
        session.add_all([challenge, skill])
        await session.flush()
        run = SolveRun(challenge_id=challenge.id, workspace_path=str(workspace))
        session.add(run)
        await session.flush()
        tool = ToolCall(run_id=run.id, tool_name="http_request")
        session.add(tool)
        await session.flush()
        artifact = Artifact(run_id=run.id, tool_call_id=tool.id, artifact_type="TEXT", file_path="evidence.txt")
        session.add(artifact)
        await session.flush()
        session.add_all([
            RunEvent(run_id=run.id, sequence=1, event_type="tool.completed"),
            Observation(run_id=run.id, tool_call_id=tool.id, artifact_id=artifact.id, observation_type="fact"),
            Hypothesis(run_id=run.id, category="test", title="test"),
            FlagCandidate(run_id=run.id, candidate="flag{test}", source_artifact_id=artifact.id),
            RunSkillSnapshot(run_id=run.id, skill_id=skill.id, skill_name=skill.name, skill_version=1, content_snapshot="test"),
        ])
        await session.commit()
        run_id = run.id

    runs_api._remove_local_workspace(str(workspace))
    assert not workspace.exists()
    async with sessions() as session:
        await runs_api._delete_run_records(session, run_id)
        await session.commit()
        assert await session.get(SolveRun, run_id) is None
        assert not (await session.scalars(select(RunEvent).where(RunEvent.run_id == run_id))).first()
    await engine.dispose()


@pytest.mark.asyncio
async def test_batch_delete_runs_reuses_full_cleanup(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    root = tmp_path / "workspaces"
    monkeypatch.setattr(runs_api, "get_settings", lambda: SimpleNamespace(workspace_root=root))

    async def fake_delete_workspace(_: str) -> None:
        return None

    monkeypatch.setattr(runs_api.runner_client, "delete_workspace", fake_delete_workspace)
    async with sessions() as session:
        challenge = Challenge(name="batch-delete", target_url="http://test.local", allowed_hosts=["test.local"])
        session.add(challenge)
        await session.flush()
        first = SolveRun(challenge_id=challenge.id, workspace_path=str(root / "first"))
        second = SolveRun(challenge_id=challenge.id, workspace_path=str(root / "second"))
        session.add_all([first, second])
        await session.flush()
        result = await runs_api.delete_runs(RunBatchDelete(run_ids=[first.id, second.id, second.id]), session)
        assert result["data"]["deleted_count"] == 2
        assert await session.get(SolveRun, first.id) is None
        assert await session.get(SolveRun, second.id) is None
    await engine.dispose()
