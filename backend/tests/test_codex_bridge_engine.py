import httpx
import pytest

from app.engines.base import EngineEvent
from app.engines.codex_bridge import CodexSdkEngine


@pytest.mark.asyncio
async def test_codex_start_triggers_first_turn_and_returns_events(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        def __init__(self, body: dict) -> None:
            self.body = body

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return self.body

    class Client:
        calls: list[str] = []

        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def post(self, url: str, **_: object) -> Response:
            self.calls.append(url)
            if url.endswith("/threads"):
                return Response({"thread_id": "bridge-thread", "status": "created"})

        def stream(self, method: str, url: str, **_: object):
            self.calls.append(url)

            class StreamResponse:
                def raise_for_status(self) -> None:
                    pass

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_: object) -> None:
                    pass

                async def aiter_lines(self):
                    yield '{"type":"tool.completed","payload":{"tool":"command_execution"}}'

            assert method == "POST"
            return StreamResponse()

    monkeypatch.setattr("app.engines.codex_bridge.httpx.AsyncClient", Client)
    events = [item async for item in CodexSdkEngine("http://bridge.test", "/workspace").start("run-1")]
    assert [item.event_type for item in events] == ["agent.message", "tool.completed"]
    assert Client.calls == ["http://bridge.test/threads", "http://bridge.test/threads/bridge-thread/run"]


@pytest.mark.asyncio
async def test_codex_start_retries_transient_thread_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    class Response:
        def __init__(self, body: dict, status_code: int = 200) -> None:
            self.body = body
            self.status_code = status_code
            self.request = httpx.Request("POST", "http://bridge.test/threads")
            self.content = b"{}"

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("temporary", request=self.request, response=httpx.Response(self.status_code, request=self.request))

        def json(self) -> dict:
            return self.body

    class Client:
        attempts = 0

        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def post(self, url: str, **_: object) -> Response:
            if url.endswith("/threads"):
                type(self).attempts += 1
                if type(self).attempts < 3:
                    raise httpx.HTTPStatusError(
                        "temporary",
                        request=httpx.Request("POST", url),
                        response=httpx.Response(503, request=httpx.Request("POST", url)),
                    )
                return Response({"thread_id": "bridge-thread", "status": "created"})
            raise AssertionError(f"unexpected url {url}")

        def stream(self, method: str, url: str, **_: object):
            class StreamResponse:
                def raise_for_status(self) -> None:
                    pass

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_: object) -> None:
                    pass

                async def aiter_lines(self):
                    yield '{"type":"tool.completed","payload":{"tool":"command_execution"}}'

            assert method == "POST"
            assert url.endswith("/run")
            return StreamResponse()

    monkeypatch.setattr("app.engines.codex_bridge.httpx.AsyncClient", Client)
    monkeypatch.setattr("app.engines.codex_bridge.asyncio.sleep", fake_sleep, raising=False)
    events = [item async for item in CodexSdkEngine("http://bridge.test", "/workspace").start("run-1")]
    assert [item.event_type for item in events] == ["agent.message", "tool.completed"]
    assert Client.attempts == 3
    assert len(sleeps) == 2


@pytest.mark.asyncio
async def test_codex_engine_reuses_persisted_thread_for_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class StreamResponse:
        def raise_for_status(self) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def aiter_lines(self):
            yield '{"type":"agent.message","payload":{"message":"continued"}}'

    class Client:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        def stream(self, method: str, url: str, **_: object):
            calls.append(url)
            assert method == "POST"
            return StreamResponse()

    monkeypatch.setattr("app.engines.codex_bridge.httpx.AsyncClient", Client)
    events = [
        item
        async for item in CodexSdkEngine(
            "http://bridge.test", "/workspace", thread_id="persisted-thread"
        ).continue_run("run-1", "补充信息")
    ]
    assert [item.event_type for item in events] == ["agent.message"]
    assert calls == ["http://bridge.test/threads/persisted-thread/run"]


@pytest.mark.asyncio
async def test_resume_recreated_thread_does_not_replay_analyzing_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = httpx.Request("POST", "http://bridge.test/threads/persisted-thread/resume")
    response = httpx.Response(404, request=request)

    async def missing_thread_stream(self, url: str, payload: dict):
        raise httpx.HTTPStatusError("thread missing", request=request, response=response)
        yield  # pragma: no cover

    async def recreated_thread(self, run_id: str, prompt: str | None = None):
        yield EngineEvent("agent.message", {"message": "recreated"}, "ANALYZING")
        yield EngineEvent("agent.turn_completed", {"message": "done"})

    engine = CodexSdkEngine(
        "http://bridge.test", "/workspace", thread_id="persisted-thread"
    )
    monkeypatch.setattr(CodexSdkEngine, "_stream_events", missing_thread_stream)
    monkeypatch.setattr(engine, "start", recreated_thread.__get__(engine, CodexSdkEngine))

    events = [item async for item in engine.resume("run-1")]
    assert [item.event_type for item in events] == ["agent.message", "agent.turn_completed"]
    assert events[0].status is None
