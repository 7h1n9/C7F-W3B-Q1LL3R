"""OpenAI Chat Completions 与 Anthropic Claude Messages 之间的双向转换。

这个模块是 :mod:`muteki.core.wire` 里 ``to_responses_request`` /
``responses_to_chat_body`` 的姊妹实现与对偶：那边在 chat 与 OpenAI *Responses*
之间来回翻译，这边在 chat 与 Claude *Messages* 之间来回翻译。两者的共同前提是
——项目内部只有一种 chat 消息表示（``role`` / ``content`` / ``tool_calls`` /
``tool_call_id``，见 ``app.solver.muteki.worker.openai_compatible`` 里 Worker 的
构造），所以转换只需要发生在 HTTP 边界上，而不是让每个调用方各写一份。

**两个方向都存在，而且服务的是不同场景**：

*上行为 chat → Claude*（``convert_chat_request``）
    内部消息要发给一个真正的 Anthropic Messages 端点时使用。这是最初为了
    「将来直连 Anthropic API」而写的方向。

*下行为 Claude → chat*（``claude_request_to_chat_body`` 等）
    这是 cc-switch 式本地代理的方向。下游 Claude 客户端（Claude Code、任何按
    Anthropic 协议编程的工具）发 ``POST /v1/messages``，而手头可用的上游中转
    只提供 OpenAI 兼容的 ``/v1/chat/completions``。代理负责把请求降级成 chat、
    把响应升回 Claude 形状，于是下游完全不知道中间换过协议。

两个方向必须成对存在，只做一半的代理会在响应阶段出错——请求转对了但下游解析
不了返回体。这也是本模块把四个函数放在一起的原因。

纯函数、无副作用、不碰数据库和网络，可以单独做单元测试。

公开 API
--------

*chat → Claude*

``to_claude_messages(messages, *, system=None) -> tuple[str | None, list[dict]]``
    把一批 chat 消息翻译成 ``(system, messages)``。``system`` 是顶层 system
    字符串（没有 system 时返回 ``None``），``messages`` 是 Claude 的
    ``user`` / ``assistant`` 严格交替列表。

``to_claude_tools(tools) -> list[dict]``
    把 OpenAI 的 ``{"type": "function", "function": {...}}`` 工具定义翻译成
    Claude 的 ``{"name", "description", "input_schema}``。

``convert_chat_request(payload, *, default_max_tokens=DEFAULT_MAX_TOKENS) -> dict``
    整包转换：吃一个 OpenAI chat 请求体，吐一个可以直接 POST 给
    ``/v1/messages`` 的请求体。

*Claude → chat*

``claude_request_to_chat_body(payload) -> dict``
    Claude 请求体 → OpenAI chat 请求体。``system`` 顶层字段变回一条
    ``role="system"`` 消息，``tool_use`` / ``tool_result`` block 还原成
    ``tool_calls`` / ``role="tool"``。

``chat_response_to_claude_body(body) -> dict``
    上游 chat 响应体 → Claude 响应体（``content`` block 数组、``stop_reason``、
    ``usage.input_tokens`` 等）。命名按「输入 → 输出」读，与上一个函数对称。

``DEFAULT_MAX_TOKENS = 4096``
    Claude **必须**有 ``max_tokens``（OpenAI 里它是可选的），调用方没给时用
    这个值兜底。

下面把几条「反直觉」的规则单独解释清楚，因为它们都是 Claude 侧硬性约束，
不是可以随便简化的实现细节。

为什么 tool_result 必须放在 user 消息里
---------------------------------------

OpenAI 有独立的 ``role="tool"``；Claude 没有这个 role。工具执行结果只能是
``user`` 消息 content 数组里的一个 ``tool_result`` block，并且
``tool_use_id`` 要对应上一轮 assistant 的 ``tool_use.id``。所以一次
``assistant(tool_calls) → tool → tool → …`` 的往返，在 Claude 侧变成
``assistant(tool_use…) → user(tool_result…)``——**多条连续的 tool 结果必须
合并进同一条 user 消息**。如果给每个 tool 结果单独开一条 user 消息，就产生了
连续的 ``user, user``，Claude 会直接报错（它要求 user/assistant 严格交替）。

为什么相邻同角色要合并
----------------------

同上：Claude 要求严格交替。转换后出现相邻同角色（例如两条连续 user，或者一条
普通 user 后面跟着一条 tool_result user）时，把它们的 content 数组合并成一条
消息的多个 block 即可。

**取舍**：合并会把「两条独立的 user 输入」变成「同一轮的多个 block」。这在
语义上确实有微小变化——模型不再能区分这是两次输入。但因为 Claude 的协议本身
就不允许相邻同角色，调用方如果坚持要区分，只能在拼装消息时自己插入一条
assistant 占位（这不是本转换器的职责）。这里选择「合并」而不是「丢弃」或
「报错」，是因为丢弃会丢信息、报错会炸掉整个请求。

为什么 arguments 是字符串，解析失败还不能炸
------------------------------------------

OpenAI 的 ``tool_calls[].function.arguments`` 是 **JSON 字符串**，Claude 的
``tool_use.input`` 是 **对象**，所以要 ``json.loads``。但模型偶尔会输出半截
JSON（被 ``max_tokens`` 截断、或者生成中途出错），如果这里抛异常，整个会话就
断了。降级策略是把原始字符串和原因塞进 input
（``{"_raw": ..., "_parse_error": ...}``）继续跑——模型下一轮能看到自己的
参数没解析成功，比直接 500 更有恢复机会。

为什么 max_tokens 是必填
------------------------

Claude 的 ``/v1/messages`` 要求显式 ``max_tokens``，缺了会 400。OpenAI 的
``max_tokens`` 是可选的，所以从 OpenAI 形状过来的请求可能没有这个字段。这里
用 :data:`DEFAULT_MAX_TOKENS`（4096）兜底。

其他约定
--------

* system 抽取：所有 ``role == "system"``（以及 OpenAI 新版的 ``developer``）
  的消息抽到顶层，若 ``system`` 形参也传了值，它排在最前，其后是消息里按出现
  顺序抽出的 system，用 ``"\n\n"`` 连接。**返回 ``None`` 表示没有 system**，
  :func:`convert_chat_request` 在这种情况下不会输出 ``system`` 键。
* 空 content 不产出空 content 数组：Claude 不接受 ``content: []``。空字符串、
  ``None``、以及「没有 tool_calls 也没有文本」的 assistant 消息都会被整条跳过。
* 多模态：``image_url`` 是 ``data:`` URL 时拆成
  ``{"type": "image", "source": {"type": "base64", ...}}``；是 ``http(s)://``
  时变成 ``{"type": "image", "source": {"type": "url", ...}}``。解析不出来的
  image part 直接丢弃（宁可少一个图，也不要发一个畸形 block 让整个请求 400）。
* 采样参数（``temperature`` / ``top_p`` 等）**刻意不做透传**，保持本模块只负责
  消息形状；调用方如果要带，自己在返回值上合并。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

#: Claude Messages API 必须显式给出 ``max_tokens``；OpenAI 侧缺省时用它兜底。
DEFAULT_MAX_TOKENS = 4096

#: Filled in for a ``tool_use`` whose ``tool_result`` never arrived. Wording is
#: deliberate: it must read as a gap the model can act on, not as an empty success.
_MISSING_TOOL_RESULT_TEXT = "No tool result was recorded for this call."

_SYSTEM_ROLES = frozenset({"system", "developer"})
_TEXT_PART_TYPES = frozenset({"text", "input_text", "output_text"})
_IMAGE_PART_TYPES = frozenset({"image_url", "input_image"})
_BASE64_IMAGE_RE = re.compile(r"^data:(?P<header>[^,]*),(?P<data>.*)$", re.DOTALL)
_WHITESPACE_RE = re.compile(r"\s+")


def to_claude_messages(
    messages: Sequence[Mapping[str, Any]] | None,
    *,
    system: str | None = None,
) -> tuple[str | None, list[dict[str, Any]]]:
    """把 OpenAI chat 消息翻译成 ``(system, claude_messages)``。

    ``system`` 为 ``None`` 表示这批消息里没有任何 system 内容——调用方据此决定
    是否在最终请求体里写出 ``system`` 键。
    """

    system_parts: list[str] = []
    if isinstance(system, str) and system.strip():
        system_parts.append(system.strip())

    raw: list[dict[str, Any]] = []
    if isinstance(messages, (list, tuple)):
        for message in messages:
            if not isinstance(message, Mapping):
                continue
            role = str(message.get("role") or "").strip().lower()
            if role in _SYSTEM_ROLES:
                text = _flatten_text(message.get("content"))
                if text:
                    system_parts.append(text)
                continue
            if role == "tool":
                # Claude 没有 tool role，工具结果只能是 user 消息里的 block。
                raw.append(
                    {"role": "user", "content": [_tool_result_block(message)]}
                )
                continue
            if role == "assistant":
                blocks = _assistant_blocks(message)
            elif role == "user":
                blocks = _normalize_content(message.get("content"))
            else:
                continue
            if not blocks:
                # 空 content 数组会让 Claude 拒绝整条请求；整条跳过。
                continue
            raw.append({"role": role, "content": blocks})

    merged = _merge_adjacent(_enforce_tool_pairing(raw))
    joined = "\n\n".join(system_parts).strip()
    return (joined or None), merged


def _enforce_tool_pairing(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep ``tool_use`` and ``tool_result`` paired, repairing each direction differently.

    Claude requires a strict bijection: every ``tool_use`` must have a
    ``tool_result`` in the next user turn, and every ``tool_result`` must name a
    ``tool_use`` that still exists. Breaking it is a hard 400 for the whole
    request, not a degraded answer.

    The two directions are not symmetric, and treating them alike is what made an
    earlier version wrong. It dropped a malformed ``tool_use`` but left its
    ``tool_result`` behind -- creating the very orphan it was trying to avoid.

    * **A result with no call** is dropped. It has nothing to attach to, and no
      placeholder could give it meaning.
    * **A call with no result** gets a synthetic result saying none was recorded.
      Dropping it instead would make the model forget it ever called the tool,
      and it would then call the same tool again -- the same failure the image
      placeholder exists to prevent. Recording the gap lets it retry knowingly.

    The synthetic text is deliberately explicit rather than empty: a model reading
    "no result was recorded" can decide to retry, whereas an empty result reads as
    a successful call that returned nothing.
    """

    answered = {
        str(block.get("tool_use_id"))
        for message in messages
        for block in message["content"]
        if block.get("type") == "tool_result"
    }
    known = {
        str(block.get("id"))
        for message in messages
        for block in message["content"]
        if block.get("type") == "tool_use"
    }

    repaired: list[dict[str, Any]] = []
    for message in messages:
        kept: list[dict[str, Any]] = []
        silent: list[str] = []
        for block in message["content"]:
            kind = block.get("type")
            if kind == "tool_result" and str(block.get("tool_use_id")) not in known:
                continue
            if kind == "tool_use" and str(block.get("id")) not in answered:
                silent.append(str(block.get("id")))
            kept.append(block)
        # A message emptied by the drop above would itself be a 400 (empty content
        # array), so it is omitted rather than emitted blank.
        if kept:
            repaired.append({"role": message["role"], "content": kept})
        if silent:
            # The answer has to sit in a user turn directly after the call, which
            # is exactly where Claude expects a tool result.
            repaired.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": call_id,
                            "content": _MISSING_TOOL_RESULT_TEXT,
                            "is_error": True,
                        }
                        for call_id in silent
                    ],
                }
            )
    return repaired


def to_claude_tools(tools: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """把 OpenAI 工具定义翻译成 Claude 的 ``name / description / input_schema``。"""

    converted: list[dict[str, Any]] = []
    if not isinstance(tools, (list, tuple)):
        return converted
    for tool in tools:
        if not isinstance(tool, Mapping):
            continue
        function = tool.get("function")
        if not isinstance(function, Mapping):
            function = tool
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        block: dict[str, Any] = {"name": name}
        description = function.get("description")
        if description:
            block["description"] = str(description)
        parameters = function.get("parameters")
        # Claude 要求 input_schema 必须是一个 JSON Schema 对象；缺失时给一个
        # 合法的空对象 schema，而不是省略这个键。
        block["input_schema"] = (
            dict(parameters)
            if isinstance(parameters, Mapping)
            else {"type": "object", "properties": {}}
        )
        converted.append(block)
    return converted


def convert_chat_request(
    payload: Mapping[str, Any] | None,
    *,
    default_max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict[str, Any]:
    """整包转换：OpenAI chat 请求体 → Claude Messages 请求体。

    输出只包含 ``model`` / ``max_tokens`` / ``system`` / ``messages`` /
    ``tools``，缺省的部分（没 system、没 tools、没 model）不会出现在结果里。
    """

    source: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
    system_text, messages = to_claude_messages(
        source.get("messages"),
        system=source.get("system") if isinstance(source.get("system"), str) else None,
    )

    body: dict[str, Any] = {}
    model = source.get("model")
    if isinstance(model, str) and model.strip():
        body["model"] = model
    elif model:
        body["model"] = str(model)

    # Claude 强制要求 max_tokens；OpenAI 侧允许缺省或给出非法值。
    max_tokens = source.get("max_tokens")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
        max_tokens = default_max_tokens
    body["max_tokens"] = int(max_tokens)

    if system_text:
        body["system"] = system_text
    body["messages"] = messages
    tools = to_claude_tools(source.get("tools"))
    if tools:
        body["tools"] = tools
    return body


def validate_claude_request(body: Mapping[str, Any]) -> list[str]:
    """Return the contract violations in a converted body, newest-first order lost.

    Exists because a converter that silently produces an unsendable body pushes
    the failure to the far end of the wire, where it arrives as an opaque HTTP 400
    with no connection to the input that caused it. That is the same silent-failure
    shape as the orphaned ``tool_result`` this module already had to fix -- the
    repair is to make the problem *visible at the boundary* instead.

    Deliberately a separate function rather than a raise inside
    :func:`convert_chat_request`: the converter is also used to normalise stored
    history, where returning an empty list is a legitimate outcome and an
    exception would be wrong. Callers that are about to send a request should call
    this first and decide for themselves.

    Returns an empty list when the body is sendable.
    """

    problems: list[str] = []
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        # Anthropic requires at least one turn. This is the one violation a caller
        # cannot fix by editing a block, so it is reported first.
        problems.append("messages must contain at least one turn")
        return problems

    if messages[0].get("role") != "user":
        problems.append(f"first turn must be user, got {messages[0].get('role')!r}")

    for index, message in enumerate(messages):
        content = message.get("content")
        if not isinstance(content, list) or not content:
            problems.append(f"turn {index} ({message.get('role')}) has empty or non-list content")
    for index in range(len(messages) - 1):
        if messages[index].get("role") == messages[index + 1].get("role"):
            problems.append(
                f"turns {index} and {index + 1} share role {messages[index].get('role')!r}"
            )

    calls: set[str] = set()
    results: set[str] = set()
    for message in messages:
        for block in message.get("content") or []:
            kind = block.get("type")
            if kind == "tool_use":
                calls.add(str(block.get("id")))
            elif kind == "tool_result":
                results.add(str(block.get("tool_use_id")))
    for orphan in sorted(results - calls):
        problems.append(f"tool_result {orphan!r} names no tool_use")
    for silent in sorted(calls - results):
        problems.append(f"tool_use {silent!r} has no tool_result")

    max_tokens = body.get("max_tokens")
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
        problems.append(f"max_tokens must be a positive integer, got {max_tokens!r}")
    return problems


def _merge_adjacent(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把相邻同角色消息合并成一条（Claude 要求 user/assistant 严格交替）。"""

    merged: list[dict[str, Any]] = []
    for message in messages:
        if merged and merged[-1]["role"] == message["role"]:
            # 连续多个 tool 结果会在这一步并进同一条 user 消息，而不是拆成
            # 多条 user——后者会产生连续同角色，Claude 会报错。
            merged[-1]["content"].extend(message["content"])
            continue
        merged.append({"role": message["role"], "content": list(message["content"])})
    return merged


def _assistant_blocks(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    """assistant 消息 → ``[text…, tool_use…]``。

    文本 block 排在 tool_use 之前，和 Claude 官方示例一致；两者都可以缺省。
    """

    blocks = _normalize_content(message.get("content"))
    blocks.extend(_tool_use_blocks(message.get("tool_calls")))
    return blocks


def _tool_use_blocks(tool_calls: Any) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if not isinstance(tool_calls, (list, tuple)):
        return blocks
    for call in tool_calls:
        if not isinstance(call, Mapping):
            continue
        function = call.get("function")
        if not isinstance(function, Mapping):
            function = {}
        name = str(function.get("name") or call.get("name") or "").strip()
        if not name:
            # 没有名字的 tool_use 无法被 Claude 关联回结果，直接丢弃。
            continue
        blocks.append(
            {
                "type": "tool_use",
                "id": str(call.get("id") or ""),
                "name": name,
                "input": _parse_arguments(function.get("arguments", call.get("arguments"))),
            }
        )
    return blocks


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """把 ``arguments`` 解析成对象；失败时降级而不是抛异常。

    模型偶尔会输出被截断的半截 JSON。抛异常会炸掉整个请求，所以这里把原始串和
    原因放进 input，让模型下一轮能自己看到参数没解析成功。
    """

    if isinstance(raw, Mapping):
        return dict(raw)
    if raw is None:
        return {}
    text = raw if isinstance(raw, str) else str(raw)
    if not text.strip():
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError) as error:
        return {"_raw": text, "_parse_error": f"{type(error).__name__}: {error}"}
    if isinstance(parsed, Mapping):
        return dict(parsed)
    return {"_raw": text, "_parse_error": "arguments JSON is not an object"}


def _tool_result_block(message: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": str(message.get("tool_call_id") or message.get("id") or ""),
        "content": _tool_result_content(message.get("content")),
    }


def _tool_result_content(value: Any) -> Any:
    """tool_result 的 content:字符串原样保留,列表转成 content block 列表。"""

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        blocks = _normalize_content(value)
        # Claude 不接受空数组;退回空字符串是有意义的"无输出"信号。
        return blocks or ""
    return str(value)


def _normalize_content(content: Any) -> list[dict[str, Any]]:
    """任意 content（字符串 / parts 列表 / 单个 part）→ content block 列表。"""

    if content is None:
        return []
    if isinstance(content, str):
        block = _text_block(content)
        return [block] if block else []
    if isinstance(content, Mapping):
        block = _part_to_block(content)
        return [block] if block else []
    if isinstance(content, (list, tuple)):
        blocks: list[dict[str, Any]] = []
        for part in content:
            if isinstance(part, str):
                block = _text_block(part)
            elif isinstance(part, Mapping):
                block = _part_to_block(part)
            else:
                block = None
            if block:
                blocks.append(block)
        return blocks
    block = _text_block(str(content))
    return [block] if block else []


def _part_to_block(part: Mapping[str, Any]) -> dict[str, Any] | None:
    kind = str(part.get("type") or "").strip().lower()
    if kind in _TEXT_PART_TYPES:
        return _text_block(part.get("text"))
    if kind in _IMAGE_PART_TYPES:
        holder = part.get("image_url")
        url = holder.get("url") if isinstance(holder, Mapping) else holder
        block = _image_block(url)
        if block is not None:
            return block
        # A malformed image degenerates to a text placeholder rather than
        # vanishing. Dropping it keeps the request valid but silently destroys
        # evidence: a solver that cannot see the attachment gets no signal that
        # anything was ever there, and will retry the same dead end. A placeholder
        # costs the model nothing and tells it plainly that an image existed and
        # was unreadable -- which is information it can act on.
        return _text_block("[image omitted: unreadable image_url]")
    # Unknown part types are still dropped: an unrecognised block would be a hard
    # 400 from Claude, and unlike an image we cannot describe what we did not
    # understand. This is the one place where dropping is the safer failure.
    return None


def _text_block(text: Any) -> dict[str, Any] | None:
    if text is None:
        return None
    if not isinstance(text, str):
        text = str(text)
    if not text:
        # 空文本不产出 block,避免拼出空 content 数组。
        return None
    return {"type": "text", "text": text}


def _image_block(url: Any) -> dict[str, Any] | None:
    """``image_url`` → Claude 的 image block。

    ``data:image/png;base64,XXXX`` 要拆成 ``media_type`` + 纯 base64；
    ``http(s)://`` 则用 Claude 的 url source。
    """

    if not isinstance(url, str):
        return None
    text = url.strip()
    if not text:
        return None
    if text.startswith("data:"):
        match = _BASE64_IMAGE_RE.match(text)
        if match is None:
            return None
        header_parts = [item.strip() for item in match.group("header").split(";")]
        media_type = header_parts[0].lower()
        if not media_type.startswith("image/"):
            return None
        if "base64" not in (item.lower() for item in header_parts[1:]):
            return None
        payload = _WHITESPACE_RE.sub("", match.group("data"))
        if not payload:
            return None
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": payload},
        }
    if text.startswith(("http://", "https://")):
        return {"type": "image", "source": {"type": "url", "url": text}}
    return None


def _flatten_text(content: Any) -> str:
    """system 消息的 content 可能是字符串或 parts，统一拍平成一个字符串。"""

    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, Mapping):
        return _flatten_text(content.get("text"))
    if isinstance(content, (list, tuple)):
        parts = [_flatten_text(item) for item in content]
        return "\n".join(part for part in parts if part)
    return str(content).strip()


# ── 反向：Claude → chat（cc-switch 式本地代理的方向）─────────────────────────
#
# 下游是 Claude 客户端，上游只有 OpenAI 兼容端点。这一组函数把请求降级、把
# 响应升回，使下游察觉不到中间换过协议。
#
# 与上行方向的关键不对称：Claude 用 block 数组表达「一次发言里的多种内容」，
# chat 用并列字段（content 字符串 + tool_calls）表达同一件事。上行是「合并」，
# 下行是「拆开」，所以这里不能简单地把上行的函数倒过来调用。

#: chat 的 finish_reason → Claude 的 stop_reason。
#: 这两个枚举不重合，直接透传会让下游按未知值处理，通常表现为「回答被截断但
#: 没有提示」。``tool_calls`` 映射成 ``tool_use`` 是必须的：Claude 客户端靠它
#: 判断该去执行工具而不是把内容当最终答案。
_STOP_REASON_BY_FINISH: dict[str, str] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "end_turn",
}

#: Claude 的 stop_reason → chat 的 finish_reason（反向，用于日志与测试对照）。
_FINISH_BY_STOP_REASON: dict[str, str] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
}


def claude_request_to_chat_body(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Claude Messages 请求体 → OpenAI chat 请求体。

    这是代理的下行方向。三个字段的落点与上行相反：

    * ``system``（顶层字符串）变回一条 ``role="system"`` 消息，排在首位。
      Claude 把它放在顶层，chat 把它当作一条消息——位置必须还原，否则上游
      会把它当成普通对话内容。
    * ``tool_use`` block 变回 assistant 的 ``tool_calls``。``input`` 已经是对象，
      要序列化成字符串，因为 chat 协议的 ``arguments`` 是 JSON 文本。
    * ``tool_result`` block 变回 ``role="tool"`` 消息。**注意**：Claude 把多个
      tool_result 放在同一条 user 消息里，而 chat 要求每个结果各占一条
      ``role="tool"`` 消息，所以这里是一对多展开，不能整条消息直接映射。

    图像 block 也在此还原成 ``image_url``，base64 要重新拼回 data URL。
    """

    source: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
    messages: list[dict[str, Any]] = []

    system_text = source.get("system")
    if isinstance(system_text, str) and system_text.strip():
        messages.append({"role": "system", "content": system_text})
    elif isinstance(system_text, (list, tuple)):
        # Claude 也允许 system 是 block 数组；拍平成一条文本即可。
        flat = _flatten_text(system_text)
        if flat:
            messages.append({"role": "system", "content": flat})

    for message in source.get("messages") or []:
        if not isinstance(message, Mapping):
            continue
        messages.extend(_claude_message_to_chat_messages(message))

    body: dict[str, Any] = {"messages": messages}

    model = source.get("model")
    if isinstance(model, str) and model.strip():
        body["model"] = model
    elif model:
        body["model"] = str(model)

    for key in ("max_tokens", "temperature", "top_p", "stream"):
        if key in source and source[key] is not None:
            body[key] = source[key]
    if "stop_sequences" in source and source["stop_sequences"] is not None:
        # 同一个概念，不同的键名。
        body["stop"] = source["stop_sequences"]
    if "metadata" in source and source["metadata"] is not None:
        body["metadata"] = source["metadata"]

    tools = _claude_tools_to_chat(source.get("tools"))
    if tools:
        body["tools"] = tools
    tool_choice = _claude_tool_choice_to_chat(source.get("tool_choice"))
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    return body


def _claude_message_to_chat_messages(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    """一条 Claude 消息 → 一条或多条 chat 消息（tool_result 会展开）。"""

    role = str(message.get("role") or "").strip().lower()
    content = message.get("content")

    # 纯字符串内容：两个协议都能直接表达。
    if isinstance(content, str):
        return [{"role": role, "content": content}] if content else []
    if not isinstance(content, (list, tuple)):
        text = _flatten_text(content)
        return [{"role": role, "content": text}] if text else []

    blocks = [block for block in content if isinstance(block, Mapping)]

    if role == "assistant":
        return _claude_assistant_to_chat(blocks)
    # user（以及任何非 assistant 角色）里可能同时有 tool_result 和普通内容。
    return _claude_user_to_chat(blocks)


def _claude_assistant_to_chat(blocks: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """assistant 的 text/tool_use blocks → 一条带 tool_calls 的 chat 消息。"""

    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in blocks:
        kind = str(block.get("type") or "").strip().lower()
        if kind == "text":
            text = _text_of(block.get("text"))
            if text:
                text_parts.append(text)
        elif kind == "tool_use":
            # chat 的 arguments 必须是 JSON 文本，而 Claude 的 input 是对象。
            arguments = block.get("input")
            if not isinstance(arguments, str):
                try:
                    arguments = json.dumps(arguments if arguments is not None else {}, ensure_ascii=False)
                except (TypeError, ValueError):
                    arguments = "{}"
            tool_calls.append(
                {
                    "id": str(block.get("id") or ""),
                    "type": "function",
                    "function": {"name": str(block.get("name") or ""), "arguments": arguments},
                }
            )
    message: dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts)}
    if tool_calls:
        message["tool_calls"] = tool_calls
    # 既无文本又无调用：不产出空消息，chat 上游会拒绝 content 为空的 assistant。
    if not text_parts and not tool_calls:
        return []
    return [message]


def _claude_user_to_chat(blocks: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """user 的 blocks → 若干 chat 消息。

    一个 Claude user 消息可以同时携带普通内容**和**多个 tool_result。chat 侧
    前者是 ``role="user"``、后者是每条一个 ``role="tool"``，所以要拆成多条。
    普通内容与工具结果保持原顺序，避免上游看到乱序的因果。
    """

    produced: list[dict[str, Any]] = []
    pending_parts: list[dict[str, Any]] = []

    def flush_pending() -> None:
        if not pending_parts:
            return
        content = _chat_content_from_claude_blocks(pending_parts)
        if content:
            produced.append({"role": "user", "content": content})
        pending_parts.clear()

    for block in blocks:
        kind = str(block.get("type") or "").strip().lower()
        if kind == "tool_result":
            # 结果必须排在它之前的所有普通内容之后，所以先冲刷。
            flush_pending()
            produced.append(
                {
                    "role": "tool",
                    "tool_call_id": str(block.get("tool_use_id") or ""),
                    "content": _tool_result_to_chat_text(block.get("content")),
                }
            )
        else:
            pending_parts.append(block)
    flush_pending()
    return produced


def _tool_result_to_chat_text(value: Any) -> str:
    """tool_result 的 content → chat 的字符串。

    chat 的 tool 消息只接受字符串。Claude 允许 block 数组（例如同时含文本和
    图片），这里只保留文本部分：图片放进 tool 消息在 chat 协议里没有对应表示，
    而丢弃整条结果会让模型失去工具执行的事实，代价更大。
    """

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        parts: list[str] = []
        for item in value:
            if isinstance(item, Mapping):
                if str(item.get("type") or "") == "text":
                    text = _text_of(item.get("text"))
                    if text:
                        parts.append(text)
                elif str(item.get("type") or "") == "image":
                    parts.append("[image omitted: chat tool results cannot carry images]")
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(value)


def _chat_content_from_claude_blocks(blocks: list[Mapping[str, Any]]) -> Any:
    """Claude 的普通内容 blocks → chat 的 content。

    纯文本时返回**字符串**而不是 parts 数组：多数 OpenAI 兼容中转对 parts
    数组的图片支持参差，纯文本走字符串是最稳的路径。只有真的含图片时才用数组。
    """

    images = [b for b in blocks if str(b.get("type") or "") == "image"]
    texts = [_text_of(b.get("text")) for b in blocks if str(b.get("type") or "") == "text"]
    texts = [t for t in texts if t]

    if not images:
        return "\n".join(texts)

    parts: list[dict[str, Any]] = []
    if texts:
        parts.append({"type": "text", "text": "\n".join(texts)})
    for image in images:
        url = _image_block_to_url(image)
        if url:
            parts.append({"type": "image_url", "image_url": {"url": url}})
    # 图片全都不可还原时退回文本，避免产出空 parts 数组。
    if not parts:
        return "\n".join(texts)
    return parts


def _image_block_to_url(block: Mapping[str, Any]) -> str | None:
    """Claude 的 image block → chat 的 image_url 字符串（base64 拼回 data URL）。"""

    source = block.get("source")
    if not isinstance(source, Mapping):
        return None
    kind = str(source.get("type") or "").strip().lower()
    if kind == "url":
        url = source.get("url")
        return str(url) if isinstance(url, str) and url else None
    if kind == "base64":
        media_type = str(source.get("media_type") or "").strip()
        data = source.get("data")
        if not media_type or not isinstance(data, str) or not data:
            return None
        return f"data:{media_type};base64,{data}"
    return None


def _claude_tools_to_chat(tools: Any) -> list[dict[str, Any]]:
    """Claude 的 ``{name, description, input_schema}`` → chat 的 function 定义。"""

    converted: list[dict[str, Any]] = []
    if not isinstance(tools, (list, tuple)):
        return converted
    for tool in tools:
        if not isinstance(tool, Mapping):
            continue
        name = str(tool.get("name") or "").strip()
        if not name:
            continue
        function: dict[str, Any] = {
            "name": name,
            # chat 侧 parameters 缺省时必须给一个合法 schema 对象，不能省略。
            "parameters": tool.get("input_schema")
            if isinstance(tool.get("input_schema"), Mapping)
            else {"type": "object", "properties": {}},
        }
        if tool.get("description"):
            function["description"] = str(tool["description"])
        converted.append({"type": "function", "function": function})
    return converted


def _claude_tool_choice_to_chat(choice: Any) -> Any:
    """Claude 的 ``tool_choice`` → chat 的 ``tool_choice``。

    Claude 用 ``{"type": "auto"|"any"|"tool", "name": ...}``，chat 用
    ``"auto"`` / ``"required"`` / ``{"type": "function", "function": {"name": ...}}``。
    取值不重合，必须映射；直接透传会让上游拒绝整个请求。
    """

    if isinstance(choice, str):
        return choice
    if not isinstance(choice, Mapping):
        return None
    kind = str(choice.get("type") or "").strip().lower()
    if kind == "auto":
        return "auto"
    if kind == "any":
        return "required"
    if kind == "none":
        return "none"
    if kind == "tool":
        name = str(choice.get("name") or "").strip()
        if name:
            return {"type": "function", "function": {"name": name}}
    return None


def chat_response_to_claude_body(body: Mapping[str, Any] | None) -> dict[str, Any]:
    """上游 chat 响应体 → Claude 响应体。

    下游 Claude 客户端按 ``content`` block 数组、``stop_reason`` 和
    ``usage.input_tokens`` 解析，所以这三处都必须重建，而不是把 chat 的字段
    原样透传。``tool_calls`` 变回 ``tool_use`` block 是其中最关键的一步——
    少了它，下游会把工具调用当成普通文本，整个 agent 循环就断了。
    """

    source: Mapping[str, Any] = body if isinstance(body, Mapping) else {}
    choice = (source.get("choices") or [{}])[0]
    if not isinstance(choice, Mapping):
        choice = {}
    message = choice.get("message") if isinstance(choice.get("message"), Mapping) else {}

    content: list[dict[str, Any]] = []
    text = _text_of(message.get("content"))
    if text:
        content.append({"type": "text", "text": text})
    for call in message.get("tool_calls") or []:
        if not isinstance(call, Mapping):
            continue
        function = call.get("function") if isinstance(call.get("function"), Mapping) else {}
        raw = function.get("arguments")
        content.append(
            {
                "type": "tool_use",
                "id": str(call.get("id") or ""),
                "name": str(function.get("name") or ""),
                # Claude 侧 input 是对象；上游给的是 JSON 文本，且可能是半截。
                "input": _parse_arguments(raw),
            }
        )

    finish = str(choice.get("finish_reason") or "stop")
    usage = source.get("usage") if isinstance(source.get("usage"), Mapping) else {}
    return {
        "id": str(source.get("id") or ""),
        "type": "message",
        "role": "assistant",
        "model": str(source.get("model") or ""),
        # Claude 不接受空 content 数组；给一个空文本 block 比给空数组安全。
        "content": content or [{"type": "text", "text": ""}],
        "stop_reason": _STOP_REASON_BY_FINISH.get(finish, "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
        },
    }


def _text_of(value: Any) -> str:
    """取文本值；非字符串原样转字符串，None 变空串。"""

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "\n".join(_text_of(item) for item in value if item is not None)
    if isinstance(value, Mapping):
        return _text_of(value.get("text"))
    return str(value)


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "chat_response_to_claude_body",
    "claude_request_to_chat_body",
    "convert_chat_request",
    "to_claude_messages",
    "to_claude_tools",
    "validate_claude_request",
]
