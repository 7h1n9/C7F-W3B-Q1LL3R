import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

BACKEND_ROOT = str(Path(__file__).resolve().parents[1])
if BACKEND_ROOT not in sys.path:
    sys.path.append(BACKEND_ROOT)

from app.solver.muteki.adapter.reason_model import (  # noqa: E402
    CoordinatorReasonModel,
)
from app.solver.muteki.coordinator import MutekiCoordinator  # noqa: E402
from app.solver.muteki.events import EventType  # noqa: E402
from app.solver.muteki.graph import MutekiGraph  # noqa: E402
from app.solver.muteki.reason import MutekiReason  # noqa: E402
from app.solver.muteki.workers import (  # noqa: E402
    EngineProfile,
    MutekiWorkerPool,
)


class _MinimalReasonEngine:
    api_key = "sk-health-test-key"
    last_trace = {}

    async def next_contract(self, messages, schema, *, name):
        raise RuntimeError(
            "MODEL_UNAVAILABLE upstream returned 503 "
            f"Authorization=Bearer {self.api_key} " + "x" * 250
        )

    async def close(self):
        return None


def test_reason_call_failure_keeps_bounded_redacted_detail() -> None:
    provider = CoordinatorReasonModel(
        _MinimalReasonEngine(),
        model_config_id="reason",
        model_name="glm-5.3-flash",
        fallback=lambda _snapshot: [],
    )

    result = asyncio.run(provider({"facts": [], "intents": [], "dead_ends": [], "flags": []}))

    assert result == []
    assert provider.last_error_code == "MODEL_UNAVAILABLE"
    assert provider.last_error_detail.startswith("MODEL_UNAVAILABLE upstream returned 503")
    assert len(provider.last_error_detail) <= 200
    assert _MinimalReasonEngine.api_key not in provider.last_error_detail


def test_health_failure_keeps_bounded_redacted_detail() -> None:
    class HealthEngine(_MinimalReasonEngine):
        async def chat(self, **_kwargs):
            raise RuntimeError(
                "MODEL_UNAVAILABLE relay returned 503 "
                f"Authorization=Bearer {self.api_key}"
            )

    provider = CoordinatorReasonModel(
        HealthEngine(),
        model_config_id="reason",
        model_name="glm-5.3-flash",
    )

    healthy, reason_code = asyncio.run(provider.health())

    assert healthy is False
    assert reason_code == "MODEL_UNAVAILABLE"
    assert provider.last_health_error_detail.startswith("MODEL_UNAVAILABLE relay returned 503")
    assert len(provider.last_health_error_detail) <= 200
    assert HealthEngine.api_key not in provider.last_health_error_detail


def test_empty_content_health_is_healthy_for_reasoning_model() -> None:
    calls = []
    recorded = []

    class ReasoningHealthEngine:
        last_trace = {"input_tokens": 4, "output_tokens": 16}

        async def chat(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(content="", reasoning_content="thinking")

    async def record(trace):
        recorded.append(trace)

    provider = CoordinatorReasonModel(
        ReasoningHealthEngine(),
        model_config_id="reason",
        model_name="glm-5.3-flash",
        usage_recorder=record,
    )
    provider.last_error_code = "MODEL_UNAVAILABLE"
    provider.last_error_detail = "relay returned 503"
    provider.consecutive_failures = 2

    healthy, reason = asyncio.run(provider.health())

    assert healthy is True
    assert reason == "HEALTHY_EMPTY_CONTENT"
    assert calls[0]["max_tokens"] == 64
    assert provider.last_error_code == ""
    assert provider.last_error_detail == ""
    assert provider.consecutive_failures == 0
    assert recorded[0]["empty_content"] is True


def test_reason_events_carry_provider_error_detail(tmp_path) -> None:
    graph = MutekiGraph(tmp_path / "reason-detail.sqlite", challenge_id="reason-detail-run")
    observed_events = []
    graph._subscriber = observed_events.append

    class Provider:
        needs_reactivation = True

        def __init__(self):
            self.calls = 0
            self.last_health_error_detail = ""

        async def health(self):
            self.calls += 1
            if self.calls == 1:
                self.last_health_error_detail = "relay returned 503"
                return False, "MODEL_UNAVAILABLE"
            self.last_health_error_detail = ""
            return True, "HEALTHY"

    provider = Provider()

    async def scenario():
        pool = MutekiWorkerPool(graph, lambda _job: None, max_workers=1)
        coordinator = MutekiCoordinator(
            graph,
            MutekiReason(provider=provider),
            pool,
            [EngineProfile("codex")],
        )
        await coordinator._retry_reason_model(provider, provider.health)
        await coordinator._retry_reason_model(provider, provider.health)

    try:
        asyncio.run(scenario())
    finally:
        graph.close()

    fallback_event = next(
        event for event in observed_events
        if event.event_type == EventType.REASON_MODEL_FALLBACK
    )
    reactivated_event = next(
        event for event in observed_events
        if event.event_type == EventType.REASON_MODEL_REACTIVATED
    )
    assert fallback_event.payload["error_detail"] == "relay returned 503"
    assert "error_detail" not in reactivated_event.payload


def test_reason_telemetry_includes_error_detail_when_available() -> None:
    provider = SimpleNamespace(
        last_error_code="MODEL_UNAVAILABLE",
        last_error_detail="relay returned 503 " + "x" * 200,
    )

    coordinator = object.__new__(MutekiCoordinator)
    coordinator.reason = MutekiReason(provider=provider)
    telemetry = coordinator._reason_provider_telemetry()

    assert telemetry["provider_error_code"] == "MODEL_UNAVAILABLE"
    assert telemetry["provider_error_detail"].startswith("relay returned 503")
    assert len(telemetry["provider_error_detail"]) == 200
