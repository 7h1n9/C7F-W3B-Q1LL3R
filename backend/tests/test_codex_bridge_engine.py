import pytest

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
