import time

import httpx
import pytest

from app.engines.openai_compatible import (
    ModelRateLimitError,
    ModelUnavailableError,
    OpenAICompatibleEngine,
)


def _chat_body() -> dict:
    return {
        "choices": [{"message": {"content": "coordinator reason"}}],
        "model": "reason-model",
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }


class _NeutralRandom:
    """A random source pinned to the midpoint, making jitter a no-op.

    The engine spreads retries with multiplicative jitter by default.  These
    tests assert the *policy* (how many retries, in what order) rather than the
    spread, so they pin the source to 0.5 and keep the deterministic delays.
    """

    def __call__(self) -> float:
        return 0.5


def _install_client(
    monkeypatch: pytest.MonkeyPatch,
    handler,
    *,
    cooldown_seconds: int = 60,
) -> tuple[OpenAICompatibleEngine, list[float], httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    engine = OpenAICompatibleEngine(
        "https://provider.test/v1",
        "secret",
        "reason-model",
        max_retries=2,
        retry_base_seconds=0.25,
        rate_limit_cooldown_seconds=cooldown_seconds,
        retry_rng=_NeutralRandom(),
    )
    monkeypatch.setattr(engine, "_get_client", lambda: client)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("app.engines.openai_compatible.asyncio.sleep", fake_sleep)
    return engine, sleeps, client


async def test_chat_retries_503_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(503, request=request)
        return httpx.Response(200, json=_chat_body(), request=request)

    engine, sleeps, client = _install_client(monkeypatch, handler)

    response = await engine.chat(
        model="reason-model",
        messages=[{"role": "user", "content": "plan"}],
    )
    await client.aclose()

    assert requests == 2
    assert sleeps == [0.25]
    assert response.content == "coordinator reason"


async def test_chat_exhausts_5xx_retries_and_raises_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(503, request=request)

    engine, sleeps, client = _install_client(monkeypatch, handler)

    with pytest.raises(ModelUnavailableError, match="provider returned HTTP 503"):
        await engine.chat(
            model="reason-model",
            messages=[{"role": "user", "content": "plan"}],
        )

    assert requests == engine.max_retries + 1
    assert sleeps == [0.25, 0.5]


async def test_chat_429_keeps_existing_cooldown_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(429, headers={"Retry-After": "0"}, request=request)

    engine, sleeps, client = _install_client(monkeypatch, handler)
    cooldown_before = time.monotonic()

    with pytest.raises(ModelRateLimitError, match="HTTP 429"):
        await engine.chat(
            model="reason-model",
            messages=[{"role": "user", "content": "plan"}],
        )

    assert requests == 1
    assert sleeps == []
    assert engine.cooldown_until > cooldown_before


async def test_chat_retries_connection_error_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json=_chat_body(), request=request)

    engine, sleeps, client = _install_client(monkeypatch, handler)

    response = await engine.chat(
        model="reason-model",
        messages=[{"role": "user", "content": "plan"}],
    )
    await client.aclose()

    assert requests == 2
    assert sleeps == [0.25]
    assert response.content == "coordinator reason"
