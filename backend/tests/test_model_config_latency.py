import asyncio
import time
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.api.v1 import models


class FakeSession:
    def __init__(self, item: SimpleNamespace) -> None:
        self.item = item
        self.commits = 0

    async def get(self, _model: object, _config_id: str) -> SimpleNamespace:
        return self.item

    async def commit(self) -> None:
        self.commits += 1


class FakeResponse:
    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        text: str = "",
        events: list[str] | None = None,
    ) -> None:
        self._payload = payload or {}
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        # The Codex probe streams and then buffers the body: a non-empty SSE
        # payload is what proves the endpoint answers the way the CLI drives it.
        self.events = events if events is not None else ['data: {"type":"response.created"}']

    async def aread(self) -> bytes:
        return (self.text or "\n".join(self.events)).encode("utf-8")

    async def aiter_lines(self):
        for line in self.events:
            yield line

    def raise_for_status(self) -> None:
        if self.status_code < 400:
            return
        request = httpx.Request("POST", "http://model.test/chat/completions")
        response = httpx.Response(
            self.status_code,
            request=request,
            headers=self.headers,
            text=self.text,
        )
        raise httpx.HTTPStatusError("probe failed", request=request, response=response)

    def json(self) -> dict[str, Any]:
        return self._payload


class SlowCapabilityClient:
    def __init__(self, *, delay: float) -> None:
        self.delay = delay
        self.calls: list[dict[str, Any]] = []
        self.max_in_flight = 0
        self._in_flight = 0
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "SlowCapabilityClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(self, _url: str, *, headers: dict[str, str], json: dict, timeout: float) -> FakeResponse:
        self.calls.append({"headers": headers, "json": json, "timeout": timeout})
        if "response_format" not in json:
            return FakeResponse({"usage": {"total_tokens": 3}})
        async with self._lock:
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            await asyncio.sleep(self.delay)
        finally:
            async with self._lock:
                self._in_flight -= 1
        return FakeResponse({"choices": [{"message": {"content": "{}"}}]})


class SlowBaseClient(SlowCapabilityClient):
    async def post(self, url: str, *, headers: dict[str, str], json: dict, timeout: float) -> FakeResponse:
        self.calls.append({"headers": headers, "json": json, "timeout": timeout})
        await asyncio.sleep(self.delay)
        return FakeResponse()

    def stream(self, method: str, url: str, *, headers: dict[str, str], json: dict, timeout: float) -> "_StreamContext":
        self.calls.append({"method": method, "url": url, "headers": headers, "json": json, "timeout": timeout})
        return _StreamContext(FakeResponse(), delay=self.delay)


class _StreamContext:
    """Stand-in for ``httpx.AsyncClient.stream()``'s async context manager."""

    def __init__(self, response: FakeResponse, *, delay: float = 0.0) -> None:
        self._response = response
        self._delay = delay

    async def __aenter__(self) -> FakeResponse:
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._response

    async def __aexit__(self, *_args: object) -> None:
        return None


class DirectProbeClient:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> "DirectProbeClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any],
        timeout: float,
    ) -> FakeResponse:
        self.calls.append(
            {"url": url, "headers": headers, "json": json, "timeout": timeout}
        )
        return self.response

    def stream(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any],
        timeout: float,
    ) -> _StreamContext:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "json": json,
                "timeout": timeout,
            }
        )
        return _StreamContext(self.response)


class FakeProcess:
    def __init__(self) -> None:
        self.killed = False
        self.communicate_calls = 0
        self.returncode: int | None = None

    async def communicate(self) -> tuple[bytes, bytes]:
        self.communicate_calls += 1
        if self.communicate_calls == 1:
            await asyncio.sleep(10)
        return b"", b""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def _model_config(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": "config-1",
        "name": "latency-test",
        "provider_type": "openai_compatible",
        "base_url": "https://provider.test/v1",
        "model_name": "slow-model",
        "encrypted_api_key": "encrypted",
        "enabled": True,
        "action_protocol": "json_schema",
        "structured_output_mode": "json_schema",
        "supports_json_schema": None,
        "supports_json_object": None,
        "supports_native_tool_call": None,
        "request_timeout_seconds": 30,
        "max_output_tokens": 256,
        "temperature": 0.0,
        "max_retries": 2,
        "retry_base_seconds": 1.0,
        "rate_limit_cooldown_seconds": 60,
        "requests_per_minute": 60,
        "max_concurrency": 2,
        "context_token_limit": 128000,
        "last_test_at": None,
        "last_test_ok": None,
        "capabilities_json": {"roles": ["worker"]},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_slow_provider_uses_shared_deadline_and_parallel_capability_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SlowCapabilityClient(delay=0.2)
    monkeypatch.setattr(models.httpx, "AsyncClient", lambda **_kwargs: client)
    monkeypatch.setattr(models, "decrypt_api_key", lambda _value: "secret")
    monkeypatch.setattr(models, "MODEL_TEST_TOTAL_BUDGET_SECONDS", 0.3)
    monkeypatch.setattr(models, "MODEL_TEST_BASE_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(models, "MODEL_TEST_PROBE_TIMEOUT_SECONDS", 0.08)
    monkeypatch.setattr(models, "MODEL_TEST_CAPABILITY_CONCURRENCY", 3)

    session = FakeSession(_model_config())
    started = time.perf_counter()
    response = await models.test_model_config("config-1", session)
    elapsed = time.perf_counter() - started

    data = response["data"]
    capabilities = data["capabilities"]
    assert elapsed < 0.6
    assert len(client.calls) == 7
    assert client.max_in_flight == 3
    assert data["timed_out"] is True
    assert data["incomplete_probe_count"] == 6
    assert capabilities["reachable"] is True
    assert capabilities["normal_chat"] is True
    assert data["ok"] is True
    assert capabilities["json_object"] is False
    assert capabilities["json_schema"] is False
    assert capabilities["agent_action"] is False
    assert capabilities["finish_action"] is False
    assert capabilities["tool_action"] is False
    assert capabilities["skill_action"] is False
    assert set(data["probe_status"].values()) == {"supported", "timeout"}
    assert data["probe_status"]["normal_chat"] == "supported"
    assert data["probe_status"]["json_schema"] == "timeout"
    assert "elapsed_ms" in data
    assert isinstance(data["message"], str)
    assert session.commits == 1


@pytest.mark.asyncio
async def test_base_timeout_is_reported_as_timeout_not_connectivity_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SlowBaseClient(delay=0.2)
    monkeypatch.setattr(models.httpx, "AsyncClient", lambda **_kwargs: client)
    monkeypatch.setattr(models, "decrypt_api_key", lambda _value: "secret")
    monkeypatch.setattr(models, "MODEL_TEST_TOTAL_BUDGET_SECONDS", 0.1)
    monkeypatch.setattr(models, "MODEL_TEST_BASE_TIMEOUT_SECONDS", 0.08)

    session = FakeSession(_model_config())
    response = await models.test_model_config("config-1", session)

    data = response["data"]
    capabilities = data["capabilities"]
    assert data["ok"] is False
    assert data["timed_out"] is True
    assert data["probe_status"] == {"normal_chat": "timeout"}
    assert capabilities["error_code"] == "MODEL_TEST_TIMEOUT"
    assert capabilities["timed_out_phase"] == "normal_chat"
    assert capabilities["error_code"] != "MODEL_UNAVAILABLE"
    assert capabilities["reachable"] is False
    assert session.commits == 1


@pytest.mark.asyncio
async def test_async_process_timeout_kills_and_reaps_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess()

    async def fake_create_subprocess_exec(*_args: object, **_kwargs: object) -> FakeProcess:
        return process

    monkeypatch.setattr(models.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    returncode, stdout, stderr, timed_out = await models._run_async_process(
        ["slow-engine"],
        timeout=0.01,
    )

    assert returncode == -1
    assert stdout == ""
    assert stderr == ""
    assert timed_out is True
    assert process.killed is True
    assert process.communicate_calls == 2


@pytest.mark.asyncio
async def test_codex_cli_timeout_preserves_response_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_process(
        _argv: list[str],
        *,
        timeout: float,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str, str, bool]:
        assert timeout == models.CODEX_CLI_PROBE_TIMEOUT_SECONDS
        assert env is None
        return -1, "", "", True

    monkeypatch.setattr(models.shutil, "which", lambda _name: "codex-test")
    monkeypatch.setattr(models, "_run_async_process", fake_run_process)
    session = FakeSession(_model_config(provider_type="codex_cli", base_url=None))

    response = await models.test_model_config("config-1", session)

    data = response["data"]
    capabilities = data["capabilities"]
    assert data["ok"] is False
    assert data["timed_out"] is True
    assert data["probe_status"] == {"codex_cli_version": "timeout"}
    assert capabilities["error_code"] == "CODEX_CLI_PROBE_TIMEOUT"
    assert capabilities["timed_out"] is True
    assert capabilities["reachable"] is False
    assert "elapsed_ms" in data


@pytest.mark.asyncio
async def test_codex_api_probe_uses_direct_responses_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DirectProbeClient(FakeResponse({"output": []}))
    captured_cli: dict[str, Any] = {}

    async def fake_run_process(
        argv: list[str],
        *,
        timeout: float,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str, str, bool]:
        captured_cli.update({"argv": argv, "timeout": timeout, "env": env})
        return 0, "codex-cli 0.148.0", "", False

    monkeypatch.setattr(models.httpx, "AsyncClient", lambda **_kwargs: client)
    monkeypatch.setattr(models.shutil, "which", lambda _name: "codex-test")
    monkeypatch.setattr(models, "_run_async_process", fake_run_process)

    ok, detail, timed_out, cli_available, error_code = (
        await models._run_codex_health_probe(
            {
                "engine": "codex",
                "base_url": "https://provider.test/v1",
                "model": "gpt-test",
                "wire_api": "responses",
            },
            {"OPENAI_API_KEY": "secret"},
            1.25,
        )
    )

    assert ok is True
    assert detail == ""
    assert timed_out is False
    assert cli_available is True
    assert error_code is None
    assert len(client.calls) == 1
    # The probe must be a streaming one: the relay answers the buffered form of
    # this exact request by hanging until the client times out.
    assert client.calls[0]["method"] == "POST"
    assert client.calls[0]["url"] == "https://provider.test/v1/responses"
    assert client.calls[0]["headers"]["Authorization"] == "Bearer secret"
    assert client.calls[0]["headers"]["Accept"] == "text/event-stream"
    assert client.calls[0]["json"] == {
        "model": "gpt-test",
        "input": "Reply with exactly: OK",
        "max_output_tokens": 512,
        "store": False,
        "stream": True,
    }
    assert client.calls[0]["timeout"] == pytest.approx(1.25, abs=0.05)
    assert captured_cli["argv"] == ["codex-test", "--version"]
    assert captured_cli["timeout"] == pytest.approx(1.25)
    assert captured_cli["env"] == {"OPENAI_API_KEY": "secret"}


@pytest.mark.asyncio
async def test_codex_endpoint_http_failure_returns_status_without_agent_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DirectProbeClient(
        FakeResponse(
            status_code=503,
            text='{"error":{"message":"Service temporarily unavailable"}}',
        )
    )
    monkeypatch.setattr(models.httpx, "AsyncClient", lambda **_kwargs: client)

    ok, detail, timed_out = await models._probe_codex_endpoint(
        base_url="https://provider.test/v1",
        api_key="secret",
        model="gpt-test",
        wire_api="responses",
        deadline=time.perf_counter() + 3.0,
    )

    assert ok is False
    assert timed_out is False
    assert "HTTP 503" in detail
    assert "Service temporarily unavailable" in detail


@pytest.mark.asyncio
async def test_codex_api_timeout_is_bounded_and_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SlowBaseClient(delay=0.2)

    async def fake_run_process(
        _argv: list[str],
        *,
        timeout: float,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str, str, bool]:
        assert timeout == 0.02
        assert env == {"OPENAI_API_KEY": "secret"}
        return 0, "codex-cli 0.148.0", "", False

    monkeypatch.setattr(models.httpx, "AsyncClient", lambda **_kwargs: client)
    monkeypatch.setattr(models.shutil, "which", lambda _name: "codex-test")
    monkeypatch.setattr(models, "_run_async_process", fake_run_process)
    monkeypatch.setattr(models, "CODEX_API_PROBE_TIMEOUT_SECONDS", 0.02)

    started = time.perf_counter()
    ok, detail, timed_out, cli_available, error_code = (
        await models._run_codex_health_probe(
            {
                "engine": "codex",
                "base_url": "https://provider.test/v1",
                "model": "gpt-test",
                "wire_api": "responses",
            },
            {"OPENAI_API_KEY": "secret"},
            0.02,
        )
    )

    assert time.perf_counter() - started < 0.15
    assert ok is False
    assert "timed out" in detail
    assert timed_out is True
    assert cli_available is True
    assert error_code == "CODEX_API_PROBE_TIMEOUT"


@pytest.mark.asyncio
async def test_capability_probe_preserves_hand_set_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe may only rewrite the keys it actually measures.

    ``route_via_proxy`` and ``model_aliases`` are set by hand, not measured.  An
    earlier version rebuilt ``capabilities_json`` from scratch, so a single Test
    click silently dropped both: the Claude CLI then stopped being routed through
    the local protocol-translating proxy, and the next solve failed with
    ``NO_HEALTHY_WORKER_PROFILE`` -- a symptom a long way from its cause.
    """

    client = SlowCapabilityClient(delay=0.0)
    monkeypatch.setattr(models.httpx, "AsyncClient", lambda **_kwargs: client)
    monkeypatch.setattr(models, "decrypt_api_key", lambda _value: "secret")

    session = FakeSession(
        _model_config(
            capabilities_json={
                "roles": ["worker"],
                "route_via_proxy": True,
                "model_aliases": {"claude-sonnet-5": "deepseek-v4.1-flash"},
            }
        )
    )
    response = await models.test_model_config("config-1", session)

    capabilities = response["data"]["capabilities"]
    assert capabilities["route_via_proxy"] is True
    assert capabilities["model_aliases"] == {"claude-sonnet-5": "deepseek-v4.1-flash"}
    # The probe still recorded what it did measure.
    assert capabilities["reachable"] is True
    assert "normal_chat" in capabilities
    assert session.commits == 1
