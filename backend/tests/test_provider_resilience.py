import httpx
import pytest

from app.engines.openai_compatible import (
    ModelRateLimitError,
    ModelUnavailableError,
    OpenAICompatibleEngine,
)


class _RateLimitResponse:
    status_code = 429
    headers = {"Retry-After": "0"}

    def raise_for_status(self) -> None:
        raise httpx.HTTPStatusError(
            "rate limited",
            request=httpx.Request("POST", "https://provider.test/v1/chat/completions"),
            response=httpx.Response(429),
        )


class _RateLimitClient:
    def __init__(self, **_: object) -> None: pass
    async def __aenter__(self): return self
    async def __aexit__(self, *_: object) -> None: pass
    async def post(self, *_: object, **__: object) -> _RateLimitResponse: return _RateLimitResponse()


class _DisconnectedClient:
    def __init__(self, **_: object) -> None: pass
    async def __aenter__(self): return self
    async def __aexit__(self, *_: object) -> None: pass
    async def post(self, *_: object, **__: object):
        raise httpx.RemoteProtocolError("Server disconnected")


class _FlakyThenSuccessClient:
    attempts = 0

    def __init__(self, **_: object) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_: object) -> None:
        pass

    async def post(self, *_: object, **__: object):
        type(self).attempts += 1
        if type(self).attempts < 3:
            response = httpx.Response(503, request=httpx.Request("POST", "https://provider.test/v1/chat/completions"))
            raise httpx.HTTPStatusError("temporary", request=response.request, response=response)
        class Response:
            def raise_for_status(self) -> None:
                pass

            def json(self) -> dict:
                return {"choices": [{"message": {"content": '{"type":"finish","result":"unsolved","summary":"No flag","flag_candidate":null}'}}]}

        return Response()


class _SkillAliasClient:
    def __init__(self, **_: object) -> None: pass

    async def __aenter__(self): return self
    async def __aexit__(self, *_: object) -> None: pass

    async def post(self, *_: object, **__: object):
        class Response:
            def raise_for_status(self) -> None:
                pass

            def json(self) -> dict:
                return {
                    "choices": [{"message": {"content": """{
                        \"type\": \"SkillAction\",
                        \"operation\": \"activate\",
                        \"phase\": \"INTAKE\",
                        \"objective\": \"Inspect backup paths\",
                        \"reason\": \"robots.txt disclosed a backup path\",
                        \"skill_identity\": {\"skill_id\": \"skill-1\", \"skill_name\": \"backup-config-leak-ctf\"},
                        \"supporting_evidence\": \"robots.txt contains /backup/\",
                        \"expected_use\": \"Probe the disclosed backup path\"
                    }"""}}]
                }

        return Response()


class _LegacyToolEnvelopeClient:
    """Provider response missing the discriminator and using old field names."""

    def __init__(self, **_: object) -> None: pass

    async def __aenter__(self): return self
    async def __aexit__(self, *_: object) -> None: pass

    async def post(self, *_: object, **__: object):
        class Response:
            def raise_for_status(self) -> None:
                pass

            def json(self) -> dict:
                return {
                    "choices": [{"message": {"content": """{
                        \"phase\": \"TESTING\",
                        \"tool_name\": \"http_request\",
                        \"arguments\": {\"url\": \"http://target.test/\"},
                        \"active_hypothesis\": \"Probe the disclosed endpoint\"
                    }"""}}]
                }

        return Response()


class _NestedToolClient:
    def __init__(self, **_: object) -> None: pass

    async def __aenter__(self): return self
    async def __aexit__(self, *_: object) -> None: pass

    async def post(self, *_: object, **__: object):
        class Response:
            def raise_for_status(self) -> None:
                pass

            def json(self) -> dict:
                return {
                    "choices": [{"message": {"content": """{
                        \"tool\": {
                            \"toolName\": \"http_request\",
                            \"params\": {\"url\": \"http://target.test/\"},
                            \"action_reason\": \"Probe nested tool object\"
                        }
                    }"""}}]
                }

        return Response()


@pytest.mark.asyncio
async def test_provider_429_is_classified_as_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_: float) -> None: pass
    monkeypatch.setattr("app.engines.openai_compatible.httpx.AsyncClient", _RateLimitClient)
    monkeypatch.setattr("app.engines.openai_compatible.asyncio.sleep", no_sleep)
    with pytest.raises(ModelRateLimitError, match="MODEL_RATE_LIMITED"):
        await OpenAICompatibleEngine("https://provider.test/v1", "secret", "model").next_action([])


@pytest.mark.asyncio
async def test_provider_disconnect_is_classified_as_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_: float) -> None: pass
    monkeypatch.setattr("app.engines.openai_compatible.httpx.AsyncClient", _DisconnectedClient)
    monkeypatch.setattr("app.engines.openai_compatible.asyncio.sleep", no_sleep)
    with pytest.raises(ModelUnavailableError, match="MODEL_UNAVAILABLE"):
        await OpenAICompatibleEngine("https://provider.test/v1", "secret", "model").next_action([])


@pytest.mark.asyncio
async def test_provider_transient_error_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    _FlakyThenSuccessClient.attempts = 0
    monkeypatch.setattr("app.engines.openai_compatible.httpx.AsyncClient", _FlakyThenSuccessClient)
    monkeypatch.setattr("app.engines.openai_compatible.asyncio.sleep", fake_sleep)
    action = await OpenAICompatibleEngine("https://provider.test/v1", "secret", "model").next_action([])
    assert action.type == "finish"
    assert _FlakyThenSuccessClient.attempts == 3
    assert len(sleeps) == 2


@pytest.mark.asyncio
async def test_provider_normalizes_skill_alias_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.engines.openai_compatible.httpx.AsyncClient", _SkillAliasClient)
    action = await OpenAICompatibleEngine("https://provider.test/v1", "secret", "model").next_action([])
    assert action.type == "skill"
    assert action.skill_id == "skill-1"
    assert action.supporting_evidence == ["robots.txt contains /backup/"]


@pytest.mark.asyncio
async def test_provider_normalizes_legacy_tool_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.engines.openai_compatible.httpx.AsyncClient", _LegacyToolEnvelopeClient)
    action = await OpenAICompatibleEngine("https://provider.test/v1", "secret", "model").next_action([])
    assert action.type == "tool"
    assert action.tool_name == "http_request"
    assert action.hypothesis == "Probe the disclosed endpoint"
    assert action.reason == "Continue the authorized investigation"


@pytest.mark.asyncio
async def test_provider_normalizes_nested_tool_object(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.engines.openai_compatible.httpx.AsyncClient", _NestedToolClient)
    action = await OpenAICompatibleEngine("https://provider.test/v1", "secret", "model").next_action([])
    assert action.type == "tool"
    assert action.tool_name == "http_request"
    assert action.arguments == {"url": "http://target.test/"}
    assert action.reason == "Probe nested tool object"
