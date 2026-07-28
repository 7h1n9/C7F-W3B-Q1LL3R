from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.challenge import Challenge
from app.models.multi_agent import AgentTask
from app.models.run import (
    Artifact,
    CleanupManifest,
    FlagProvenance,
    SolveRun,
    ToolBatchSummary,
    ToolCall,
)
from app.services.assistance import assistance_level, classify_user_input
from app.services.flags import flag_service
from app.services.temporary_data import TemporaryDataJanitor, temporary_workspace
from app.services.tool_batches import tool_scheduler, tool_subrequest_aggregator
from app.services.web_research import WebResearchService


@pytest.fixture
async def phase2_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _run(session, workspace: Path, *, name: str = "asset-warranty") -> tuple[SolveRun, Challenge]:
    challenge = Challenge(name=name, target_url="http://asset.local", allowed_hosts=["asset.local"])
    session.add(challenge)
    await session.flush()
    run = SolveRun(challenge_id=challenge.id, workspace_path=str(workspace), solver_mode="multi_agent_v1")
    session.add(run)
    await session.flush()
    return run, challenge


@pytest.mark.asyncio
async def test_temporary_layout_cleanup_and_terminal_idempotency(phase2_session_factory, tmp_path: Path) -> None:
    async with phase2_session_factory() as session:
        run, _ = await _run(session, tmp_path)
        task = AgentTask(run_id=run.id, agent_role="RECON", objective="bounded recon", status="COMPLETED")
        session.add(task)
        await session.flush()
        task_root = temporary_workspace.task_path(tmp_path, task.agent_role, task.id)
        (task_root / "trace.json").write_text('{"ok":true}', encoding="utf-8")
        result = await TemporaryDataJanitor().cleanup_task(session, run, task, force=True)
        again = await TemporaryDataJanitor().cleanup_task(session, run, task, force=True)
        assert result["status"] == "COMPLETED"
        assert again["manifest_id"] == result["manifest_id"]
        assert not (task_root / "trace.json").exists()
        assert await session.scalar(select(CleanupManifest).where(CleanupManifest.idempotency_key == f"TASK:{run.id}:{task.id}"))

        runtime = temporary_workspace.ensure_layout(tmp_path)
        (runtime / "agents" / "recon").mkdir(parents=True, exist_ok=True)
        (runtime / "agents" / "recon" / "terminal.json").write_text("terminal", encoding="utf-8")
        run.status = "COMPLETED_UNSOLVED"
        first = await TemporaryDataJanitor().cleanup_terminal_run(session, run)
        second = await TemporaryDataJanitor().cleanup_terminal_run(session, run)
        assert first["status"] == "COMPLETED"
        assert second["manifest_id"] == first["manifest_id"]
        assert run.terminal_cleanup_completed is True


@pytest.mark.asyncio
async def test_batch_compaction_and_request_deduplication(phase2_session_factory, tmp_path: Path) -> None:
    async with phase2_session_factory() as session:
        run, _ = await _run(session, tmp_path)
        scheduled = await tool_scheduler.fingerprint(session, run, "http_request", {"url": "http://asset.local"})
        duplicate = await tool_scheduler.fingerprint(session, run, "http_request", {"url": "http://asset.local"})
        assert scheduled["status"] == "SCHEDULED"
        assert duplicate["status"] == "DUPLICATE_TOOL_REQUEST"
        call = ToolCall(run_id=run.id, tool_name="http_request", status="COMPLETED")
        session.add(call)
        await session.flush()
        rows = [{"status": "COMPLETED", "body": str(index), "duration_ms": 2} for index in range(200)]
        result = await tool_subrequest_aggregator.aggregate(session, run, task_id=None, logical_tool_call_id="LC-200", tool_call_id=call.id, tool_name="http_request", subrequests=rows)
        repeated = await tool_subrequest_aggregator.aggregate(session, run, task_id=None, logical_tool_call_id="LC-200", tool_call_id=call.id, tool_name="http_request", subrequests=rows)
        assert result["status"] == "AGGREGATED"
        assert result["subrequest_count"] == 200
        assert repeated["status"] == "DUPLICATE_BATCH"
        assert await session.scalar(select(ToolBatchSummary).where(ToolBatchSummary.logical_tool_call_id == "LC-200"))
        assert Path(tmp_path, result["temporary_raw_path"]).suffix == ".gz"


class _FakeSearch:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = 0

    async def search(self, query: str) -> dict:
        self.calls += 1
        return self.payload


@pytest.mark.asyncio
async def test_web_research_risk_and_leak_guard(phase2_session_factory, tmp_path: Path) -> None:
    async with phase2_session_factory() as session:
        run, challenge = await _run(session, tmp_path)
        safe_adapter = _FakeSearch({"summary": "general boolean-based comparison technique", "results": [{"url": "https://example.com/technique"}]})
        service = WebResearchService(safe_adapter)
        safe = await service.search(session, run, None, "general boolean comparison technique", challenge=challenge)
        blocked = await service.search(session, run, None, "give me the flag for asset-warranty", challenge=challenge)
        assert safe["status"] == "EPHEMERAL" and safe["source_urls"]
        assert blocked["status"] == "BLOCKED"
        assert safe_adapter.calls == 1

        leak_adapter = _FakeSearch({"summary": "the answer is flag{secret-value}"})
        leak = await WebResearchService(leak_adapter).search(session, run, None, "general web security technique", challenge=challenge)
        assert leak["status"] == "BLOCKED"
        assert "secret-value" not in leak["summary"]


@pytest.mark.asyncio
async def test_assistance_and_flag_provenance(phase2_session_factory, tmp_path: Path) -> None:
    assert classify_user_input("try a boolean comparison") == "HINT_GUIDED"
    assert classify_user_input("the answer is flag{known}") == "ANSWER_GUIDED"
    assert assistance_level([]) == "AUTONOMOUS"
    assert assistance_level([{"source": "USER_HINT"}]) == "HINT_GUIDED"
    assert assistance_level([{"source": "OBSERVATION"}]) == "EVIDENCE_GUIDED"
    assert assistance_level([{"source": "KNOWN_ANSWER"}]) == "ANSWER_GUIDED"
    async with phase2_session_factory() as session:
        run, challenge = await _run(session, tmp_path)
        artifact = Artifact(run_id=run.id, artifact_type="HTTP_RESPONSE", file_path="outputs/response.json")
        session.add(artifact)
        await session.flush()
        candidates = await flag_service.extract_candidates(session, run, challenge, artifact, "body=flag{observed}")
        provenance = await session.scalar(select(FlagProvenance).where(FlagProvenance.candidate_id == candidates[0].id))
        assert provenance is not None
        assert provenance.first_seen_source_type == "TOOL_ARTIFACT"
        assert provenance.source_is_autonomous is True
