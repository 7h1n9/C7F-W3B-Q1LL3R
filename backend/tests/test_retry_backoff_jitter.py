"""Backoff jitter: spreads concurrent retries without changing the policy.

The coordinator-reason outage review called for jittered retries against
provider 5xx, because a deterministic ``base * 2**attempt`` delay makes every
concurrent Run retry on the same tick and turns a short provider hiccup into a
self-inflicted thundering herd.
"""

import random

import httpx
import pytest

from app.engines.openai_compatible import OpenAICompatibleEngine
from app.engines.retry import DEFAULT_RETRY_JITTER, backoff_delay


class _ScriptedRandom:
    """Yield a fixed sequence so jitter assertions stay deterministic."""

    def __init__(self, *values: float) -> None:
        self._values = list(values)

    def __call__(self) -> float:
        return self._values.pop(0) if self._values else 0.5


def test_backoff_delay_default_is_unchanged() -> None:
    """The legacy deterministic contract must survive untouched."""

    assert backoff_delay(0) == 0.75
    assert backoff_delay(0, base=0.25) == 0.25
    assert backoff_delay(1, base=0.25) == 0.5
    assert backoff_delay(9, base=0.25, cap=15.0) == 15.0


def test_backoff_delay_jitter_stays_inside_band() -> None:
    jitter = 0.5
    for attempt in range(5):
        base_value = backoff_delay(attempt, base=0.25, cap=15.0)
        for draw in (0.0, 0.25, 0.5, 0.75, 1.0):
            value = backoff_delay(
                attempt,
                base=0.25,
                cap=15.0,
                jitter=jitter,
                rng=_ScriptedRandom(draw),
            )
            assert value >= max(base_value * (1 - jitter) - 1e-9, 0.0)
            assert value <= min(base_value * (1 + jitter) + 1e-9, 15.0)


def test_backoff_delay_jitter_never_undercuts_retry_after() -> None:
    """Retry-After is a provider contract, not a hint."""

    value = backoff_delay(
        0,
        base=0.25,
        cap=15.0,
        retry_after=8.0,
        jitter=0.5,
        rng=_ScriptedRandom(0.0),  # would otherwise shrink to 4.0
    )
    assert value == 8.0


def test_backoff_delay_jitter_decorrelates_concurrent_runs() -> None:
    rng = random.Random(20261006)
    delays = [
        backoff_delay(2, base=1.0, cap=15.0, jitter=DEFAULT_RETRY_JITTER, rng=rng.random)
        for _ in range(32)
    ]
    assert len(set(delays)) > 20
    assert min(delays) >= 4.0 * (1 - DEFAULT_RETRY_JITTER)
    assert max(delays) <= 4.0 * (1 + DEFAULT_RETRY_JITTER)


async def test_engine_defaults_to_jittered_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare engine (production wiring) must already be jittered."""

    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(503, request=request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "recovered"}}],
                "model": "reason-model",
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    engine = OpenAICompatibleEngine(
        "https://provider.test/v1",
        "secret",
        "reason-model",
        retry_base_seconds=0.25,
    )
    monkeypatch.setattr(engine, "_get_client", lambda: client)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("app.engines.openai_compatible.asyncio.sleep", fake_sleep)

    response = await engine.chat(
        model="reason-model",
        messages=[{"role": "user", "content": "plan"}],
    )
    await client.aclose()

    assert engine.retry_jitter == DEFAULT_RETRY_JITTER
    assert requests == 2
    assert response.content == "recovered"
    assert len(sleeps) == 1
    # 0.25 * 2**0 = 0.25 with +/-50% spread applied.
    assert 0.125 <= sleeps[0] <= 0.375


def test_engine_accepts_a_pinned_random_source() -> None:
    engine = OpenAICompatibleEngine(
        "https://provider.test/v1",
        "secret",
        "model",
        retry_base_seconds=0.25,
        retry_rng=_ScriptedRandom(0.5),
    )
    assert engine._backoff(1, base=0.25, cap=15.0) == 0.5


def test_engine_clamps_a_bad_jitter_ratio() -> None:
    engine = OpenAICompatibleEngine(
        "https://provider.test/v1",
        "secret",
        "model",
        retry_jitter=7.0,
    )
    assert engine.retry_jitter == 1.0
