"""OpenAI *Responses* 代理：让 codex CLI 能用只提供 chat 的中转。

与 :mod:`app.api.claude_proxy` 严格对称——那边是「Anthropic Messages 客户端
遇到只讲 chat 的中转」，这边是「Responses 客户端（codex CLI）遇到只讲 chat
的中转」。两者都是 cc-switch 式本地代理，都放在服务端紧贴凭据，都由客户端
各自的固定路径触发。

为什么路径是 ``/v1/responses`` 而不是 ``/api/v1/...``
-----------------------------------------------------

路径是**契约**不是偏好：codex CLI 把 ``/responses`` 拼到它配置的 base_url
后面，文档改不了这一点。挂在应用的 ``/api/v1`` 前缀下会让它对自己服务的
工具不可达——这与 claude 侧 ``/v1/messages`` 被刻意放在 ``app.api.v1``
之外是同一个理由。

实测依据（codex 0.148，本机二进制 + 真实 CLI 抓包）
--------------------------------------------------

* codex 的二进制里 ``chat_completions`` 出现 **0 次**，只有
  ``responses`` / ``responses_websocket``——这就是它无法直连 chat 中转的
  根本原因，也是把 ``wire_api`` 钉死为 ``responses`` 的依据。
* 请求形状：顶层 ``model`` / ``input`` / ``stream:true``，系统提示走顶层
  ``instructions`` 或多条 ``role="developer"`` 消息；工具**两处都出现过**
  （顶层扁平 ``tools`` 与 ``input[].additional_tools`` 的 namespace 嵌套）。
* 响应形状：codex 接受 ``response.created`` → ``response.in_progress`` →
  逐块 ``…added`` / ``…delta`` / ``…done`` → ``response.completed`` 的序列。
  缺 ``output_item.done`` 或索引对不上，它会重连（日志里反复出现
  ``Reconnecting``）。

上游选择
--------

调用方（codex）只会在请求体里带模型名，没有别的定位信息。这里按
:func:`_resolve_upstream` 的顺序解析：别名命中 → 模型名命中 → 名字命中 →
第一个 enabled 配置。回落而不是报错，是因为 codex 无法得知中转的模型目录。

与 claude 侧的一处**刻意差异**：这里**不做** ``model_aliases`` 之外的名字
翻译。codex 发的是它自己配置里的模型名（``-c model=...`` 由平台注入，本来
就是上游的真名），不需要「官方名 → 真名」那一层；而 claude CLI 只会发它
内置的 ``claude-sonnet-5``，所以那边必须有别名表。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_session
from app.core.exceptions import DomainError
from app.core.net import outbound_trust_env
from app.models.model_config import ModelConfig
from app.services.codex_message_converter import (
    ResponsesStreamTranslator,
    chat_response_to_responses_body,
    responses_request_to_chat_body,
)
from app.services.crypto import decrypt_api_key
from muteki.core.wire import resolve_endpoint

logger = logging.getLogger(__name__)

router = APIRouter(tags=["codex-proxy"])

UPSTREAM_TIMEOUT = 300.0

#: 建立连接的上限。``UPSTREAM_TIMEOUT`` 同时管着读，靠它等到 300 秒才发现连不上。
UPSTREAM_CONNECT_TIMEOUT = 10.0

#: 等上游**第一个字节**的上限。
#:
#: ``UPSTREAM_TIMEOUT`` 是整段交换的上限（一次推理可以正当地流好几分钟），
#: 所以它不能兼作「卡死探测器」——实测它也确实没起到这个作用：上游偶尔
#: 接受连接后一个字节都不发（同一份请求重发一次 4.4 秒就正常），代理却会
#: 抱着这条 SSE 空等 300 秒。Worker 的体检预算是 120 秒，于是每次撞上都判
#: ``WORKER_HEALTHCHECK_FAILED``，整轮 Run 静默降级成单 lane。
#:
#: 所以按「首字节」单独设预算：卡住 → 20 秒内放弃 → 重发。这条路径上什么都
#: 还没交给客户端，重发是免费的。
UPSTREAM_FIRST_BYTE_TIMEOUT = 20.0

#: 首字节之前允许重发几次（含第一次）。
UPSTREAM_ATTEMPTS = 2

#: 这些上游状态码值得在「还没吐出内容」时重发一次。
#:
#: 实测同一份请求在数秒内会在 429 与 200 之间摇摆（中转的限流是按瞬时并发算的），
#: 一次立即重试就能穿过去；而把 429 直接抛给 codex 只会让它整轮作废。
_RETRYABLE_UPSTREAM_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Responses 的错误形状与 chat/Anthropic 都不同：codex 读 ``error.message``
#: 与 ``error.type``，并且靠 HTTP 状态决定是否重试。原样透传上游的 chat 错误
#: 也能被它读出来（字段名恰好一致），所以这里只做「保形状」不做重映射；这个
#: 映射表存在的意义是给**代理自己**产生的错误一个正确的 type。
_ERROR_TYPE_BY_STATUS = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    500: "server_error",
    502: "server_error",
    503: "server_error",
}


def _error(status: int, message: str) -> JSONResponse:
    """Responses 形状的错误体（codex 按 ``error.message`` 显示）。"""

    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "type": _ERROR_TYPE_BY_STATUS.get(status, "server_error"),
                "message": message,
            }
        },
    )


def _aliases(config: ModelConfig) -> dict[str, str]:
    """``请求里的模型名 -> 上游真名``，读 ``capabilities_json.model_aliases``。"""

    capabilities = config.capabilities_json if isinstance(config.capabilities_json, dict) else {}
    raw = capabilities.get("model_aliases")
    if not isinstance(raw, Mapping):
        return {}
    # A blank key is dropped as well as a blank value: an empty model name is
    # never a real request, and keeping it would let ``""`` match every lookup
    # that normalises to empty.
    return {
        str(key).strip(): str(value).strip()
        for key, value in raw.items()
        if str(key).strip() and str(value).strip()
    }


async def _resolve_upstream(session: AsyncSession, requested_model: str | None) -> ModelConfig:
    """按「别名 → 模型名 → 配置名 → 第一个 enabled」选出上游配置。"""

    enabled = list(
        (
            await session.scalars(
                select(ModelConfig).where(ModelConfig.enabled.is_(True)).order_by(ModelConfig.name)
            )
        ).all()
    )
    if not enabled:
        raise DomainError(
            "CODEX_PROXY_NO_UPSTREAM",
            "No enabled model configuration is available to serve this request.",
            status_code=503,
            stage="CODEX_PROXY",
        )
    wanted = str(requested_model or "").strip().lower()
    if wanted:
        # 别名是运营者的显式意图，优先级高于名字匹配。
        for config in enabled:
            if wanted in {key.lower() for key in _aliases(config)}:
                return config
        for config in enabled:
            if str(config.model_name or "").strip().lower() == wanted:
                return config
        for config in enabled:
            if config.name.strip().lower() == wanted:
                return config
    # 回落而非报错：codex 无法得知中转的模型目录，要求精确名字会让代理不可用。
    return enabled[0]


def _resolve_upstream_model(config: ModelConfig, requested: str | None) -> str:
    """请求里的模型名 → 上游真名（别名优先，否则用配置自己的 model_name）。"""

    real = str(config.model_name or config.name).strip()
    wanted = str(requested or "").strip()
    if not wanted:
        return real
    alias = {key.lower(): value for key, value in _aliases(config).items()}.get(wanted.lower())
    return alias or real


def _authorised(request: Request) -> bool:
    """有 token 时校验共享密钥。

    只有 launcher 生成了 token 才强制——它把代理绑到 ``0.0.0.0`` 以便 Worker
    容器经 host 别名访问，同时意味着局域网可达，所以必须有个密钥挡着。
    用 :func:`hmac.compare_digest`，普通 ``==`` 会比出密钥长度与前缀。
    """

    expected = str(get_settings().codex_proxy_token or "").strip()
    if not expected:
        return True
    supplied = (
        request.headers.get("x-api-key")
        or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        or ""
    ).strip()
    return bool(supplied) and hmac.compare_digest(supplied, expected)


@router.post("/v1/responses")
async def codex_responses(request: Request, session: AsyncSession = Depends(get_session)):
    """接受一个 Responses 请求，用 chat 上游服务它。"""

    if not _authorised(request):
        return _error(401, "Invalid or missing proxy token.")

    try:
        payload: Mapping[str, Any] = await request.json()
    except ValueError:
        return _error(400, "Request body is not valid JSON.")
    if not isinstance(payload, Mapping):
        return _error(400, "Request body must be a JSON object.")

    config = await _resolve_upstream(session, payload.get("model"))
    api_key = decrypt_api_key(config.encrypted_api_key) if config.encrypted_api_key else ""
    endpoint = resolve_endpoint(config.base_url)

    chat_body = responses_request_to_chat_body(payload)
    # 请求里是 codex 配置的模型名或别名，上游只认它自己的真名。
    chat_body["model"] = _resolve_upstream_model(config, payload.get("model"))

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # codex 一律流式（它的解析器不接受非流式回收）。非流式分支仍留着，
    # 供探针与手工验证使用——那时把整个 chat 响应体升成 Responses 形状。
    if not payload.get("stream"):
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(UPSTREAM_TIMEOUT, connect=UPSTREAM_CONNECT_TIMEOUT),
            trust_env=outbound_trust_env(),
        ) as client:
            try:
                response = await client.post(endpoint, headers=headers, json=chat_body)
            except httpx.HTTPError as error:
                return _error(502, f"Upstream is unreachable: {error}")
        if not response.is_success:
            return _error(response.status_code, _upstream_message(response))
        try:
            upstream_body = response.json()
        except ValueError:
            return _error(502, "Upstream returned a non-JSON body.")
        return JSONResponse(
            content=chat_response_to_responses_body(
                upstream_body, model=str(chat_body.get("model") or "")
            )
        )

    return StreamingResponse(
        _stream_responses_events(endpoint, headers, chat_body),
        media_type="text/event-stream",
    )


def _upstream_message(response: httpx.Response) -> str:
    """尽量取上游自己的报错文案；取不到就用截断的正文。"""

    try:
        detail = response.json()
    except ValueError:
        return response.text[:400] or "Upstream request failed."
    if isinstance(detail, Mapping):
        error = detail.get("error")
        if isinstance(error, Mapping) and error.get("message"):
            return str(error["message"])
        if detail.get("message"):
            return str(detail["message"])
    return str(detail)[:400] or "Upstream request failed."


async def _stream_responses_events(
    endpoint: str, headers: dict[str, str], chat_body: Mapping[str, Any]
) -> AsyncIterator[str]:
    """把上游 chat SSE 翻成 Responses SSE。

    翻译由 :class:`ResponsesStreamTranslator` 负责（含索引记账）；这里只管
    HTTP：连上游、逐行读、把事件按 Responses 的 wire 形状编码出去。
    """

    translator = ResponsesStreamTranslator(model=str(chat_body.get("model") or ""))

    async def encode(event: str, data: dict[str, Any]) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    for event, data in translator.start():
        yield await encode(event, data)

    def failure(message: str, error_type: str = "server_error") -> str:
        """一条明确的 ``response.failed``。

        codex 对半路断掉的流会一直重连，而一个明确的失败事件让它立刻收尾。
        """

        return "event: response.failed\ndata: " + json.dumps(
            {
                "type": "response.failed",
                "response": {
                    "id": translator.response_id,
                    "status": "failed",
                    "error": {"type": error_type, "message": message},
                },
            },
            ensure_ascii=False,
        ) + "\n\n"

    #: 已经交出过内容就不能再重发了——客户端会把两份答案拼起来。重发只在
    #: 「上游还没吐出任何 chat 分片」时进行，也就是这条 lane 最容易卡住的那一段。
    delivered = False

    for attempt in range(1, UPSTREAM_ATTEMPTS + 1):
        # 每次都现算，不要在循环开头取一次快照：``delivered`` 会在本轮中途
        # 变 True，快照会让「已交付内容」的那一轮照样重发。
        stalled = False
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(UPSTREAM_TIMEOUT, connect=UPSTREAM_CONNECT_TIMEOUT),
                trust_env=outbound_trust_env(),
            ) as client:
                async with client.stream(
                    "POST", endpoint, headers=headers, json=chat_body
                ) as response:
                    if not response.is_success:
                        body = (await response.aread()).decode("utf-8", "replace")[:400]
                        if attempt < UPSTREAM_ATTEMPTS and response.status_code in _RETRYABLE_UPSTREAM_STATUS:
                            logger.warning(
                                "codex proxy: upstream %s on attempt %d/%d, retrying",
                                response.status_code,
                                attempt,
                                UPSTREAM_ATTEMPTS,
                            )
                            continue
                        yield failure(
                            body or "Upstream streaming request failed.",
                            _ERROR_TYPE_BY_STATUS.get(response.status_code, "server_error"),
                        )
                        return
                    lines = response.aiter_lines()
                    awaiting_first = True
                    while True:
                        try:
                            if awaiting_first:
                                line = await asyncio.wait_for(
                                    lines.__anext__(), UPSTREAM_FIRST_BYTE_TIMEOUT
                                )
                            else:
                                line = await lines.__anext__()
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError:
                            stalled = True
                            break
                        awaiting_first = False
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except ValueError:
                            continue
                        if not isinstance(chunk, Mapping):
                            continue
                        delivered = True
                        for event, payload in translator.push(chunk):
                            yield await encode(event, payload)
        except httpx.HTTPError as error:
            if attempt < UPSTREAM_ATTEMPTS and not delivered:
                logger.warning(
                    "codex proxy: upstream error on attempt %d/%d (%s), retrying",
                    attempt,
                    UPSTREAM_ATTEMPTS,
                    error,
                )
                continue
            yield failure(f"Upstream stream failed: {error}")
            return

        if stalled:
            if attempt < UPSTREAM_ATTEMPTS and not delivered:
                logger.warning(
                    "codex proxy: upstream sent nothing for %.0fs on attempt %d/%d, retrying",
                    UPSTREAM_FIRST_BYTE_TIMEOUT,
                    attempt,
                    UPSTREAM_ATTEMPTS,
                )
                continue
            yield failure(
                f"Upstream sent nothing for {UPSTREAM_FIRST_BYTE_TIMEOUT:.0f}s."
            )
            return
        break

    for event, data in translator.finish():
        yield await encode(event, data)
    # chat 的 ``[DONE]`` 在这里补：翻译器产的事件是 Responses 语义，哨兵不是。
    yield "data: [DONE]\n\n"