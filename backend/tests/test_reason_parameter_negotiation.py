"""A Reason model that pins one parameter value must not disable Reason.

Measured on 2026-10-20, run ``e4320288`` (Weblogic CVE-2017-10271): the relay
answers ``400 {"error":{"message":"field temperature invalid, only 1 is allowed
for this model","type":"invalid_request_error"}}``.  The Reason planner asks for
``temperature=0.3`` and the health probe for ``0.0``, so every Reason call died
and the Coordinator fell back to deterministic strategy — which is why the
correct knowledge prior (``cve-2017-10271-weblogic-ws-at``, score 115.27) never
reached a Worker and ``wsl-wsat`` got zero hits.

Every assertion here is falsifiable: each test fails when the negotiation is
removed from ``_ParameterNegotiatingEngine``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

BACKEND_ROOT = str(Path(__file__).resolve().parents[1])
if BACKEND_ROOT not in sys.path:
    sys.path.append(BACKEND_ROOT)

from app.engines.openai_compatible import ModelProviderError  # noqa: E402
from app.solver.muteki.adapter.reason_model import (  # noqa: E402
    CoordinatorReasonModel,
    _ParameterNegotiatingEngine,
)

KIMI_BODY = (
    '{"error":{"message":"field temperature invalid, only 1 is allowed for '
    'this model","type":"invalid_request_error"}}'
)


def _status_error(status: int, body: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://relay.example/v1/chat/completions")
    return httpx.HTTPStatusError(
        "rejected",
        request=request,
        response=httpx.Response(status, text=body, request=request),
    )


class _PinningEngine:
    """Rejects any temperature except 1, exactly like the Kimi relay."""

    def __init__(self) -> None:
        self.api_key = "sk-reason-test"
        self.last_trace: dict = {}
        self.temperatures: list[float] = []
        self.calls = 0

    async def chat(self, **kwargs):
        self.calls += 1
        self.temperatures.append(float(kwargs.get("temperature", -1)))
        if kwargs.get("temperature") != 1:
            # The adapter sees it wrapped, as in production.
            raise ModelProviderError(
                "MODEL_BAD_REQUEST", "provider rejected the request"
            ) from _status_error(400, KIMI_BODY)
        return SimpleNamespace(content="OK", input_tokens=1, output_tokens=1, model="kimi-k3")

    async def close(self) -> None:
        return None


def test_health_recovers_when_the_model_pins_temperature() -> None:
    engine = _PinningEngine()
    provider = CoordinatorReasonModel(
        engine,
        model_config_id="reason",
        model_name="kimi-k3",
        fallback=None,
    )

    healthy, code = asyncio.run(provider.health())

    assert healthy is True, "a pinned parameter value must not be read as an outage"
    assert code == "HEALTHY"
    # Falsifiable: without negotiation the first (and only) call keeps 0.0.
    assert engine.temperatures == [0.0, 1.0], engine.temperatures


def test_the_negotiated_value_is_remembered_across_calls() -> None:
    engine = _PinningEngine()
    wrapper = _ParameterNegotiatingEngine(engine)

    async def scenario() -> None:
        await wrapper.chat(model="kimi-k3", messages=[], temperature=0.3)
        await wrapper.chat(model="kimi-k3", messages=[], temperature=0.3)
        await wrapper.chat(model="kimi-k3", messages=[], temperature=0.0)

    asyncio.run(scenario())

    # One rejection, then every later call already carries the allowed value.
    assert engine.calls == 4, engine.calls
    assert engine.temperatures == [0.3, 1.0, 1.0, 1.0], engine.temperatures
    assert wrapper.negotiations == ["temperature=1.0"], wrapper.negotiations


def test_a_rejection_that_names_no_parameter_is_not_retried() -> None:
    class _AuthEngine(_PinningEngine):
        async def chat(self, **kwargs):
            self.calls += 1
            raise ModelProviderError(
                "MODEL_PERMISSION_DENIED", "provider returned HTTP 403"
            ) from _status_error(403, '{"code":"GROUP_DISABLED"}')

    engine = _AuthEngine()
    wrapper = _ParameterNegotiatingEngine(engine)

    with pytest.raises(ModelProviderError) as raised:
        asyncio.run(wrapper.chat(model="kimi-k3", messages=[], temperature=0.3))

    assert raised.value.code == "MODEL_PERMISSION_DENIED"
    # Falsifiable: a blanket retry would spend a second request here.
    assert engine.calls == 1, engine.calls
    assert wrapper.negotiations == []


def test_a_field_we_cannot_change_is_not_retried() -> None:
    class _NoValueEngine(_PinningEngine):
        async def chat(self, **kwargs):
            self.calls += 1
            raise ModelProviderError(
                "MODEL_BAD_REQUEST", "provider rejected the request"
            ) from _status_error(400, '{"error":{"message":"field seed invalid"}}')

    engine = _NoValueEngine()
    wrapper = _ParameterNegotiatingEngine(engine)

    with pytest.raises(ModelProviderError):
        asyncio.run(wrapper.chat(model="kimi-k3", messages=[], temperature=0.3))

    # The engine always writes the field, so "drop it" is not a retry we can do.
    assert engine.calls == 1, engine.calls


def test_attributes_still_reach_the_wrapped_engine() -> None:
    engine = _PinningEngine()
    wrapper = _ParameterNegotiatingEngine(engine)

    assert wrapper.api_key == "sk-reason-test"
    assert wrapper.last_trace == {}
    asyncio.run(wrapper.close())
