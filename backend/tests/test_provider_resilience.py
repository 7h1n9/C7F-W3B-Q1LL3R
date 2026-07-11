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
