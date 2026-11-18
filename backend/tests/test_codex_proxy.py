"""回归：Codex Responses 代理（端点、鉴权、上游解析、接线）。

这些测试锁的是「代理独立成立」与「和运行时接线一致」两件事。真实的 codex
CLI 端到端验证在功能落地时已手工跑过（codex → 代理 → 只支持 chat 的中转，
文本与工具调用都通），这里覆盖的是它无法自动化回归的部分：错误形状、鉴权、
上游选择顺序、以及 ``route_via_proxy`` 打开时端点确实被替换。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.api.codex_proxy import _aliases, _authorised, _resolve_upstream_model, _upstream_message
from app.core.config import codex_proxy_endpoint, get_settings
from app.solver.muteki.runtime import muteki_runtime


class _FakeRequest:
    """Minimal stand-in for a Starlette request (only headers are read)."""

    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {}


class _FakeConfig:
    def __init__(
        self, *, name: str, model_name: str = "", capabilities: dict | None = None
    ) -> None:
        self.name = name
        self.model_name = model_name
        self.capabilities_json = capabilities or {}


@pytest.fixture(autouse=True)
def _restore_settings():
    """Keep per-test settings mutations from leaking between tests."""

    settings = get_settings()
    saved = (settings.codex_proxy_token, settings.codex_proxy_state_file)
    yield
    settings.codex_proxy_token, settings.codex_proxy_state_file = saved


# -- 鉴权 ------------------------------------------------------------------- #


def test_no_token_configured_means_no_check() -> None:
    # A token nobody generated is not a secret; the launcher only mints one
    # because it binds 0.0.0.0. A loopback-only manual run has none.
    get_settings().codex_proxy_token = ""
    assert _authorised(_FakeRequest()) is True


def test_a_matching_bearer_token_passes() -> None:
    get_settings().codex_proxy_token = "s3cret"
    assert _authorised(_FakeRequest({"authorization": "Bearer s3cret"})) is True


def test_a_matching_x_api_key_passes() -> None:
    get_settings().codex_proxy_token = "s3cret"
    assert _authorised(_FakeRequest({"x-api-key": "s3cret"})) is True


def test_a_wrong_token_is_rejected() -> None:
    get_settings().codex_proxy_token = "s3cret"
    assert _authorised(_FakeRequest({"authorization": "Bearer nope"})) is False


def test_a_missing_token_is_rejected_when_one_is_required() -> None:
    get_settings().codex_proxy_token = "s3cret"
    assert _authorised(_FakeRequest()) is False


# -- 模型别名与上游名解析 ---------------------------------------------------- #


def test_aliases_are_read_from_capabilities() -> None:
    config = _FakeConfig(
        name="c", capabilities={"model_aliases": {"alias-a": "real-a", "": "ignored"}}
    )
    assert _aliases(config) == {"alias-a": "real-a"}


def test_aliases_are_empty_without_the_key() -> None:
    assert _aliases(_FakeConfig(name="c")) == {}


def test_an_alias_wins_over_the_configs_own_model() -> None:
    config = _FakeConfig(
        name="c", model_name="real-a", capabilities={"model_aliases": {"alias-a": "real-b"}}
    )
    assert _resolve_upstream_model(config, "alias-a") == "real-b"


def test_an_unknown_model_falls_back_to_the_configs_model() -> None:
    # codex sends the model it was configured with; the stored model_name is
    # what the upstream actually answers to.
    config = _FakeConfig(name="c", model_name="deepseek-v4.1-flash")
    assert _resolve_upstream_model(config, "gpt-5.6-sol") == "deepseek-v4.1-flash"


def test_an_absent_model_uses_the_configs_model() -> None:
    config = _FakeConfig(name="c", model_name="m")
    assert _resolve_upstream_model(config, None) == "m"


def test_the_config_name_is_the_last_resort() -> None:
    config = _FakeConfig(name="named", model_name="")
    assert _resolve_upstream_model(config, None) == "named"


# -- 上游错误文案 ------------------------------------------------------------ #


class _FakeResponse:
    def __init__(self, *, json_body: Any = None, text: str = "") -> None:
        self._json = json_body
        self.text = text

    def json(self) -> Any:
        if self._json is None:
            raise ValueError("not json")
        return self._json


def test_the_upstreams_own_message_is_preferred() -> None:
    # Reporting the relay's own text is what makes a 404 "本站不提供 Responses"
    # readable instead of a generic failure.
    response = _FakeResponse(json_body={"error": {"message": "本站不提供 Responses 协议"}})
    assert "Responses" in _upstream_message(response)


def test_a_non_json_body_degrades_to_truncated_text() -> None:
    assert _upstream_message(_FakeResponse(text="boom")) == "boom"


def test_a_success_shaped_body_without_a_message_still_reports_something() -> None:
    assert _upstream_message(_FakeResponse(json_body={"detail": "x"}, text="raw")) != ""


# -- 代理端点读取（状态文件） ------------------------------------------------- #


def test_a_missing_state_file_is_not_a_proxy(tmp_path) -> None:
    get_settings().codex_proxy_state_file = str(tmp_path / "absent.json")
    assert codex_proxy_endpoint() is None


def test_a_state_file_pointing_at_a_dead_port_is_ignored(tmp_path) -> None:
    # A launcher killed with SIGKILL never cleans up, so a stale file survives.
    # Trusting it would turn every Worker turn into a connection timeout that
    # looks like an upstream outage.
    import json
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = int(probe.getsockname()[1])
    path = tmp_path / "codex-proxy.json"
    path.write_text(
        json.dumps({"port": closed_port, "host_base_url": f"http://127.0.0.1:{closed_port}"}),
        encoding="utf-8",
    )
    get_settings().codex_proxy_state_file = str(path)
    assert codex_proxy_endpoint() is None


def test_the_claude_and_codex_state_files_are_distinct() -> None:
    # Sharing one file would make each reader see whichever proxy wrote last.
    settings = get_settings()
    assert settings.codex_proxy_state_file != settings.claude_proxy_state_file


# -- 运行时接线：route_via_proxy -------------------------------------------- #


def _config_with_proxy(enabled: bool) -> _FakeConfig:
    return _FakeConfig(name="c", capabilities={"route_via_proxy": enabled})


def test_no_proxy_url_when_the_flag_is_off() -> None:
    assert muteki_runtime._codex_proxy_base_url(_config_with_proxy(False), "upstream_local") is None


def test_no_proxy_url_when_no_proxy_is_running(monkeypatch) -> None:
    # Falls back to the stored base_url, so the failure is the upstream's own
    # protocol error rather than a timeout against a dead port.
    monkeypatch.setattr(muteki_runtime, "codex_proxy_endpoint", lambda: None)
    assert muteki_runtime._codex_proxy_base_url(_config_with_proxy(True), "upstream_local") is None


def test_a_container_worker_gets_the_host_alias(monkeypatch) -> None:
    # A container's 127.0.0.1 is its own loopback, not the host's.
    monkeypatch.setattr(
        muteki_runtime,
        "codex_proxy_endpoint",
        lambda: {
            "port": 45678,
            "host_base_url": "http://127.0.0.1:45678",
            "container_base_url": "http://host.docker.internal:45678",
        },
    )
    container = next(iter(muteki_runtime.CONTAINER_WORKER_BACKENDS))
    assert (
        muteki_runtime._codex_proxy_base_url(_config_with_proxy(True), container)
        == "http://host.docker.internal:45678"
    )


def test_a_host_worker_gets_the_loopback_url(monkeypatch) -> None:
    monkeypatch.setattr(
        muteki_runtime,
        "codex_proxy_endpoint",
        lambda: {
            "port": 45678,
            "host_base_url": "http://127.0.0.1:45678",
            "container_base_url": "http://host.docker.internal:45678",
        },
    )
    assert (
        muteki_runtime._codex_proxy_base_url(_config_with_proxy(True), "not-a-container")
        == "http://127.0.0.1:45678"
    )


def test_the_claude_and_codex_resolvers_are_independent(monkeypatch) -> None:
    # Both read the same flag but must resolve different proxies: pointing one
    # at the other's port would make every turn fail inside a container.
    monkeypatch.setattr(
        muteki_runtime, "claude_proxy_endpoint", lambda: {"port": 1111, "host_base_url": "http://127.0.0.1:1111"}
    )
    monkeypatch.setattr(
        muteki_runtime, "codex_proxy_endpoint", lambda: {"port": 2222, "host_base_url": "http://127.0.0.1:2222"}
    )
    config = _config_with_proxy(True)
    assert muteki_runtime._claude_proxy_base_url(config, "host") == "http://127.0.0.1:1111"
    assert muteki_runtime._codex_proxy_base_url(config, "host") == "http://127.0.0.1:2222"


# -- 代理应用装配 ------------------------------------------------------------ #


def test_the_proxy_app_exposes_only_the_responses_route() -> None:
    # The child must not import app.main, or every child would enter the same
    # lifespan and start a grandchild -- an unbounded process tree.
    from app.api.codex_proxy import router

    paths = {route.path for route in router.routes}
    assert paths == {"/v1/responses"}


def test_the_proxy_route_is_mounted_without_the_api_prefix() -> None:
    # codex appends /responses to the base_url it is handed, so the path is
    # fixed by the client and cannot be relocated under /api/v1.  Checked via
    # the OpenAPI schema because router inclusion wraps the routes.
    from app.main import app

    paths = set(app.openapi().get("paths", {}))
    assert "/v1/responses" in paths
    # Mounted alongside the Claude one, which is the same kind of contract path.
    assert "/v1/messages" in paths


def test_the_supervisor_is_wired_into_the_backend_lifespan() -> None:
    # A codex channel that needs the proxy must have it started with the
    # backend, mirroring the Claude one.
    import app.main as main

    assert hasattr(main, "codex_proxy_supervisor")


def test_resolving_an_upstream_with_no_enabled_config_is_a_domain_error() -> None:
    from app.api.codex_proxy import _resolve_upstream
    from app.core.exceptions import DomainError

    class _EmptySession:
        async def scalars(self, *_args, **_kwargs):
            class _Result:
                def all(self_inner):
                    return []

            return _Result()

    with pytest.raises(DomainError):
        asyncio.run(_resolve_upstream(_EmptySession(), "m"))


# ── 上游卡死时不能把整条 lane 拖垮（2026-10-26）─────────────────────────────


class _FakeStream:
    """``client.stream(...)`` 的上下文管理器形状。"""

    def __init__(self, response: Any) -> None:
        self._response = response

    async def __aenter__(self) -> Any:
        return self._response

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeClient:
    """按顺序返回预设响应，并记录被调用了几次。"""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def stream(self, *args: object, **kwargs: object) -> _FakeStream:
        index = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return _FakeStream(self._responses[index])

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _chat_chunk(text: str) -> dict[str, Any]:
    return {
        "id": "c1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }


def _done_chunk() -> dict[str, Any]:
    return {
        "id": "c1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }


class _StreamingResponse:
    def __init__(self, lines: list[str], *, then_raise: Exception | None = None) -> None:
        self.status_code = 200
        self.is_success = True
        self._lines = lines
        self._then_raise = then_raise

    async def aread(self) -> bytes:
        return b""

    async def aiter_lines(self):
        for line in self._lines:
            yield line
        if self._then_raise is not None:
            raise self._then_raise
        await asyncio.sleep(30)  # 之后不再有任何字节：模拟上游挂住


class _StalledResponse(_StreamingResponse):
    def __init__(self) -> None:
        super().__init__([], then_raise=None)

    async def aiter_lines(self):
        await asyncio.sleep(30)  # 一个字节都不发
        yield "data: never"  # pragma: no cover


class _RateLimitedResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.is_success = False

    async def aread(self) -> bytes:
        return b'{"error":{"message":"rate limited"}}'


async def _collect(monkeypatch: pytest.MonkeyPatch, responses: list[Any], budget: float = 0.05):
    from app.api import codex_proxy

    client = _FakeClient(responses)
    monkeypatch.setattr(codex_proxy.httpx, "AsyncClient", lambda *a, **k: client)
    monkeypatch.setattr(codex_proxy, "UPSTREAM_FIRST_BYTE_TIMEOUT", budget)
    chunks: list[str] = []
    async for chunk in codex_proxy._stream_responses_events(
        "http://upstream.invalid/v1/chat/completions", {}, {"model": "m", "stream": True}
    ):
        chunks.append(chunk)
    return chunks, client


async def test_a_stalled_upstream_is_retried_before_anything_reaches_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空等 300 秒会把「引擎挂了」写进 Run，而引擎其实是好的。

    2026-10-26 实测（run ``d81f754b`` ``9calc``）：同一份请求，第一次 90 秒只
    收到本地生成的 ``response.created`` / ``in_progress``，第二次 4.4 秒正常收流。
    代理当时用 ``timeout=UPSTREAM_TIMEOUT``(300s) 一把抓，于是卡住就一直卡；
    Worker 的体检预算只有 120 秒，于是判 ``WORKER_HEALTHCHECK_FAILED``，
    整轮静默降级成单 lane —— 上游没坏，是代理不会放弃。

    重发之所以安全：这条路径上还没有任何一个 chat 分片交给客户端。
    """

    stalled = _StalledResponse()
    healthy = _StreamingResponse(
        [
            "data: " + json.dumps(_chat_chunk("OK")),
            "data: " + json.dumps(_done_chunk()),
            "data: [DONE]",
        ]
    )
    chunks, client = await _collect(monkeypatch, [stalled, healthy])

    body = "".join(chunks)
    assert client.calls == 2, "首批字节超时必须重发一次"
    assert "response.failed" not in body
    assert "response.completed" in body


async def test_a_retry_only_happens_before_content_reaches_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已经吐出内容就不能重发：客户端会把两份答案拼起来。"""

    import httpx

    partial = _StreamingResponse(
        ["data: " + json.dumps(_chat_chunk("half an answer"))],
        then_raise=httpx.ReadError("connection dropped"),
    )
    healthy = _StreamingResponse(["data: " + json.dumps(_chat_chunk("OK")), "data: [DONE]"])
    chunks, client = await _collect(monkeypatch, [partial, healthy])

    assert client.calls == 1, "内容已经交付，不得重发"
    assert "response.failed" in "".join(chunks)


async def test_a_rate_limited_upstream_is_retried_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """中转的限流按瞬时并发算，同一请求数秒内就会从 429 变 200。"""

    limited = _RateLimitedResponse(429)
    healthy = _StreamingResponse(
        ["data: " + json.dumps(_chat_chunk("OK")), "data: [DONE]"]
    )
    chunks, client = await _collect(monkeypatch, [limited, healthy])

    body = "".join(chunks)
    assert client.calls == 2
    assert "response.completed" in body
    assert "429" not in body
