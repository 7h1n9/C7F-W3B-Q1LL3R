import asyncio
import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.engines.openai_compatible import OpenAICompatibleEngine
from app.models.base import Base
from app.models.challenge import Challenge
from app.models.run import RunEvent, SolveRun
from app.orchestration.orchestrator import SolveOrchestrator
from app.schemas.agent import AgentAction, FinishAction
from app.schemas.system_settings import ServiceSettingsUpdate
from app.services.crypto import decrypt_api_key, encrypt_api_key
from app.services.events import EventService


def test_agent_action_rejects_extra_fields() -> None:
    adapter = TypeAdapter(AgentAction)
    with pytest.raises(ValidationError):
        adapter.validate_python({"type": "tool", "tool_name": "http_request", "arguments": {}, "reason": "inspect", "shell": "whoami"})


def test_api_key_is_encrypted_and_round_trips() -> None:
    secret = "sk-test-secret"
    encrypted = encrypt_api_key(secret)
    assert encrypted != secret
    assert decrypt_api_key(encrypted) == secret


def test_service_settings_allow_only_local_endpoints() -> None:
    settings = ServiceSettingsUpdate(runner_url="http://127.0.0.1:8091", codex_bridge_url="http://localhost:8090")
    assert str(settings.runner_url).startswith("http://127.0.0.1")
    with pytest.raises(ValidationError):
        ServiceSettingsUpdate(runner_url="http://example.com", codex_bridge_url="http://localhost:8090")


@pytest.mark.asyncio
async def test_openai_engine_repairs_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = ["not-json", json.dumps({"type": "finish", "result": "unsolved", "summary": "No flag found", "flag_candidate": None})]

    class Response:
        def raise_for_status(self) -> None: pass
        def json(self) -> dict: return {"choices": [{"message": {"content": responses.pop(0)}}]}

    class Client:
        def __init__(self, **_: object) -> None: pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_: object) -> None: pass
        async def post(self, *_: object, **__: object) -> Response: return Response()

    monkeypatch.setattr("app.engines.openai_compatible.httpx.AsyncClient", Client)
    action = await OpenAICompatibleEngine("http://provider.test/v1", "secret", "model").next_action([{"role": "user", "content": "go"}])
    assert action.type == "finish"
    assert action.result == "unsolved"


@pytest.mark.asyncio
async def test_concurrent_events_have_unique_sequences(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        challenge = Challenge(name="test", target_url="http://test.local", allowed_hosts=["test.local"])
        session.add(challenge); await session.flush()
        run = SolveRun(challenge_id=challenge.id, workspace_path=str(tmp_path))
        session.add(run); await session.commit()
        run_id = run.id
    service = EventService()
    async def append(index: int) -> None:
        async with sessions() as session:
            await service.append(session, run_id, "test.event", {"index": index})
    await asyncio.gather(*(append(index) for index in range(8)))
    async with sessions() as session:
        sequences = list((await session.scalars(select(RunEvent.sequence).where(RunEvent.run_id == run_id).order_by(RunEvent.sequence))).all())
    assert sequences == list(range(1, 9))
    await engine.dispose()


@pytest.mark.asyncio
async def test_openai_loop_verifies_flag_and_generates_report(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'loop.db'}", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    workspace = tmp_path / "workspace"; workspace.mkdir()
    async with sessions() as session:
        challenge = Challenge(name="loop", description="test", target_url="http://test.local", allowed_hosts=["test.local"], flag_pattern=r"flag\{[^}]+\}")
        session.add(challenge); await session.flush()
        run = SolveRun(challenge_id=challenge.id, engine_type="openai_compatible", workspace_path=str(workspace))
        session.add(run); await session.commit()
        run_id = run.id

    class ScriptedEngine:
        async def next_action(self, _messages: list[dict]) -> FinishAction:
            return FinishAction(type="finish", result="solved", summary="Flag is present", flag_candidate="flag{verified}")

    monkeypatch.setattr("app.orchestration.orchestrator.SessionLocal", sessions)
    await SolveOrchestrator(engine_factory=lambda _: ScriptedEngine()).start(run_id)
    async with sessions() as session:
        completed = await session.get(SolveRun, run_id)
        assert completed.status == "COMPLETED_SOLVED", completed.last_error_message
    assert (workspace / "final" / "writeup.md").is_file()
    await engine.dispose()


@pytest.mark.asyncio
async def test_openai_compatible_tool_to_artifact_flag_e2e(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'e2e.db'}", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    workspace = tmp_path / "workspace"; workspace.mkdir()
    async with sessions() as session:
        challenge = Challenge(name="e2e", description="test", target_url="http://challenge.local", allowed_hosts=["challenge.local"], flag_pattern=r"flag\{[^}]+\}")
        session.add(challenge); await session.flush()
        run = SolveRun(challenge_id=challenge.id, engine_type="openai_compatible", workspace_path=str(workspace), max_agent_steps=3)
        session.add(run); await session.commit()
        run_id = run.id

    actions = [
        json.dumps({"type": "tool", "tool_name": "http_request", "arguments": {"method": "GET", "url": "http://challenge.local/"}, "reason": "Inspect the authorized target", "hypothesis_id": None}),
        json.dumps({"type": "finish", "result": "solved", "summary": "Found a flag", "flag_candidate": "flag{e2e}"}),
    ]
    state: dict[str, str] = {}

    class Response:
        def __init__(self, body: dict) -> None: self.body = body
        def raise_for_status(self) -> None: pass
        def json(self) -> dict: return self.body

    class Client:
        def __init__(self, **_: object) -> None: pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_: object) -> None: pass
        async def post(self, url: str, **kwargs: object) -> Response:
            if "provider.test" in url:
                return Response({"choices": [{"message": {"content": actions.pop(0)}}]})
            payload = kwargs["json"]
            assert isinstance(payload, dict)
            workspace_path = Path(str(payload["workspace_path"]))
            artifact = workspace_path / "responses" / "http_0001.json"
            artifact.parent.mkdir(exist_ok=True); artifact.write_text('{"body":"flag{e2e}"}', encoding="utf-8")
            state["artifact"] = "responses/http_0001.json"
            return Response({"job_id": "runner-job"})
        async def get(self, _url: str, **_: object) -> Response:
            return Response({"job_id": "runner-job", "status": "COMPLETED", "result": {"artifact_path": state["artifact"], "summary": "HTTP 200", "status_code": 200}})

    monkeypatch.setattr("app.engines.openai_compatible.httpx.AsyncClient", Client)
    monkeypatch.setattr("app.orchestration.orchestrator.SessionLocal", sessions)
    await SolveOrchestrator(engine_factory=lambda _: OpenAICompatibleEngine("http://provider.test/v1", "secret", "test-model")).start(run_id)
    async with sessions() as session:
        completed = await session.get(SolveRun, run_id)
        assert completed.status == "COMPLETED_SOLVED", completed.last_error_message
    assert (workspace / "responses" / "http_0001.json").is_file()
    assert (workspace / "final" / "writeup.md").is_file()
    await engine.dispose()
