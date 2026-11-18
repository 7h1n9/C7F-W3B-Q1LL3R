"""OpenAI *Responses* 与 Chat Completions 之间的双向转换（codex 代理用）。

为什么需要它
------------

这一对转换是 :mod:`muteki.core.wire` 里 ``to_responses_request`` /
``responses_to_chat_body`` 的**下游代理版**，也是
:mod:`app.services.claude_message_converter` 的平行实现：

``muteki.core.wire`` 里的那两个函数服务于「内部只有 chat 表示，要去打一个
只支持 Responses 的上游」——那是 **上行**（caller 是 chat 引擎）。

本模块服务于**下行**：Worker 是一个 codex CLI，它只会讲 Responses
（实测 codex 0.148 的二进制里 ``chat_completions`` 出现 0 次，只有
``responses`` / ``responses_websocket``），而手头可用的中转大多只提供
``/v1/chat/completions``。中间缺一层翻译时，codex 的每一次请求都被上游以

    {"error":{"message":"本站不提供 Responses(reasoning) 协议，请改用 POST /v1/chat/completions"}}

挡回来——这正是本地代理存在的全部理由。与 Claude 侧 ``route_via_proxy``
完全对称：那边翻译 Anthropic Messages ↔ chat，这边翻译 Responses ↔ chat。

为什么不能直接复用 ``muteki.core.wire``
---------------------------------------

* ``to_responses_request`` 是 chat → Responses，本模块需要的是**反方向**。
* 但反方向也**不能**简单地把 ``responses_to_chat_body`` 拿来做请求转换：
  后者转换的是**响应体**，而这里要转的是**请求体**。两者形状相似却不同
  （请求有 ``input`` 而响应有 ``output``）。
* 最关键的是**工具声明形状变了**。实测 codex 0.148 会在两处声明工具：

  1. 顶层 ``tools``，是扁平的 Responses 形状
     ``{"type":"function","name":...,"parameters":{...}}``；
  2. ``input`` 里一个 ``{"type":"additional_tools"}`` 项，工具按
     ``namespace`` 分组嵌套（``functions`` / ``collaboration``），里面有
     ``function`` 也有**自由格式的 ``custom``**（``exec`` 工具，用 Lark 文法
     约束一段 JavaScript 源码）。

  扁平 ``tools`` 与嵌套 ``additional_tools`` 必须**都**被接受，否则一次
  工具调用会静默退化成正文——实测过：只读前者时，模型把
  `` <invoke name="shell"> `` 当文本吐了出来，而不是返回结构化 tool_calls。

为什么 ``developer`` 必须降级成 ``system``
------------------------------------------

codex 把系统指令放在 ``role="developer"``（OpenAI 新角色）的多条消息里。
严格的中转会直接拒收：

    developer is not one of ['system', 'assistant', 'user', 'tool', 'function']

所以降级为 ``system``，并把相邻同角色消息**合并**（多条 system 同样会被
一部分中转拒收）。这个「相邻合并」与 claude 侧的理由不同：Claude 是**协议
要求** user/assistant 严格交替；chat 的多数中转无此要求，但它的校验器把
重复的 system 也当异常。代价很小——模型少了一层「这是第几条系统消息」的
区分，而系统提示的语义不变。

公开 API
--------

``responses_request_to_chat_body(payload, *, default_model="") -> dict``
    codex 的 Responses 请求体 → chat 请求体（``input`` → ``messages``，
    两处工具声明展开成 ``tools``）。

``chat_response_to_responses_body(body, *, model="") -> dict``
    上游 chat **非流式**响应体 → Responses 响应体。代理的流式路径不走这里，
    它逐事件合成；但一条非流式往返（含 4xx 之外的正常回收）需要它。

``chat_chunk_to_responses_events(chunk) -> list[tuple[str, dict]]``
    上游 chat SSE 的**单个** chunk → 零到多条 Responses SSE 事件。
    有状态：调用方需要为每次请求建一个 :class:`ResponsesStreamTranslator`
    并把事件按序 yield。

``ResponsesStreamTranslator``
    把 chat 的分片流翻译成 codex 认识的事件序列。``message_start`` 等
    开闭事件由 :meth:`ResponsesStreamTranslator.start` /
    :meth:`finish` 产生，中间内容由 :meth:`push` 产生。

设计约束（与 claude 转换器一致）
--------------------------------

纯函数 + 一个显式状态对象，不碰数据库与网络，可单独单元测试。转换必须
**无损**地保住 ``call_id``：codex 靠它把工具结果对回上一次调用，丢了它
下一轮就配对不上。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

#: 上游 chat 上游未必认识 ``max_output_tokens``；这只是兜底上限，避免
#: 一次转换漏掉 max_tokens 时请求变成无限制输出。
DEFAULT_MAX_OUTPUT_TOKENS = 8192

_SYSTEM_ROLES = frozenset({"system", "developer"})

#: ``custom`` 工具（codex 的 ``exec``）用文法约束自由格式源码，chat 侧没有
#: 对应形状。折成一个单字符串参数，既不丢调用能力，也不伪装成 JSON 对象。
_CUSTOM_TOOL_INPUT_KEY = "input"


def _text_of(value: Any) -> str:
    """把 Responses 的 content（字符串 / block 列表 / 对象）拉平成文本。

    与 ``muteki.core.wire._text_of`` 同构但独立：那个函数是 vendored 的私有
    实现，本模块不能依赖它，也不该在语义上被它的改动牵着走。
    """

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("text", "content", "output"):
            if key in value:
                return _text_of(value[key])
        return ""
    if isinstance(value, (list, tuple)):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping):
                text = _text_of(item)
                if text:
                    parts.append(text)
        return "".join(parts)
    return str(value)


def _flatten_tools(entries: Any) -> list[dict[str, Any]]:
    """把两种工具声明形状统一成 chat 的 ``{"type":"function",...}`` 列表。

    接受：扁平 Responses 形状、``namespace`` 分组、``custom`` 自由格式工具。
    递归一层 namespace 就够——实测 codex 0.148 只嵌一层（``functions`` /
    ``collaboration``），但递归不为零成本，多一层也不会坏。
    """

    flat: list[dict[str, Any]] = []

    def walk(items: Any) -> None:
        for entry in items or []:
            if not isinstance(entry, Mapping):
                continue
            kind = str(entry.get("type") or "")
            if kind == "namespace":
                walk(entry.get("tools"))
                continue
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            description = str(entry.get("description") or "")
            if kind == "custom":
                flat.append(
                    {
                        "type": "function",
                        "function": {
                            "name": name,
                            "description": description,
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    _CUSTOM_TOOL_INPUT_KEY: {
                                        "type": "string",
                                        "description": "自由格式输入（原始源码）",
                                    }
                                },
                                "required": [_CUSTOM_TOOL_INPUT_KEY],
                            },
                        },
                    }
                )
                continue
            parameters = entry.get("parameters")
            flat.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": description,
                        "parameters": parameters
                        if isinstance(parameters, Mapping)
                        else {"type": "object", "properties": {}},
                    },
                }
            )

    walk(entries)
    return flat


def _merge_adjacent(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """合并相邻的纯文本同角色消息。

    与 claude 侧同名函数形似而理由不同：那边是 Anthropic 协议硬性要求
    user/assistant 交替；这里是因为一部分中转把重复的 ``system`` 视为异常
    （codex 会连发多条 ``developer``，降级后就是多条 ``system``）。
    带 ``tool_calls`` 的 assistant 消息绝不参与合并——它必须紧跟自己的
    ``role="tool"`` 结果，被吸进别的消息会让配对失效。
    """

    merged: list[dict[str, Any]] = []
    for message in messages:
        previous = merged[-1] if merged else None
        if (
            previous is not None
            and previous.get("role") == message.get("role")
            and isinstance(previous.get("content"), str)
            and isinstance(message.get("content"), str)
            and "tool_calls" not in previous
            and "tool_calls" not in message
            and previous["role"] in _SYSTEM_ROLES
        ):
            previous["content"] = f"{previous['content']}\n\n{message['content']}"
            continue
        merged.append(dict(message))
    return merged


def responses_request_to_chat_body(
    payload: Mapping[str, Any], *, default_model: str = ""
) -> dict[str, Any]:
    """codex 的 ``/v1/responses`` 请求体 → ``/v1/chat/completions`` 请求体。

    ``input`` 数组里可能有四种项：``additional_tools``（工具声明）、
    ``message``（对话轮）、``function_call``（上一轮的工具调用）、
    ``function_call_output``（工具结果）。四种都要还原成 chat 的对应形状，
    否则多轮工具往返会在第二轮断掉。
    """

    messages: list[dict[str, Any]] = []

    #: Responses 把系统提示放在顶层 ``instructions``，chat 放在一条 system
    #: 消息里。它必须排在 ``input`` 之前——中转让它在后时，模型的指令遵循
    #: 会明显变差（同 claude 侧的取舍）。
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})

    tools: list[dict[str, Any]] = []
    # 顶层扁平声明与嵌套声明都可能出现，实测两种都出现过，必须都收。
    tools.extend(_flatten_tools(payload.get("tools")))

    for item in payload.get("input") or []:
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("type") or "")

        if kind == "additional_tools":
            tools.extend(_flatten_tools(item.get("tools")))
            continue

        if kind == "message":
            role = str(item.get("role") or "user")
            if role in _SYSTEM_ROLES:
                # 见模块 docstring：严格的中转拒收 ``developer``。
                role = "system"
            content = _text_of(item.get("content"))
            if not content:
                continue
            messages.append({"role": role, "content": content})
            continue

        if kind == "function_call":
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": str(item.get("call_id") or item.get("id") or ""),
                            "type": "function",
                            "function": {
                                "name": str(item.get("name") or ""),
                                "arguments": str(item.get("arguments") or "{}"),
                            },
                        }
                    ],
                }
            )
            continue

        if kind == "function_call_output":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(item.get("call_id") or item.get("id") or ""),
                    "content": _text_of(item.get("output")),
                }
            )
            continue

        # ``reasoning`` / ``custom_tool_call`` 等其余项对 chat 上游无意义，
        # 静默跳过而不是报错：多一个未知项不该让整轮失败。
        continue

    chat: dict[str, Any] = {
        "model": str(payload.get("model") or default_model or "").strip(),
        "messages": _merge_adjacent(messages),
        # codex 一律流式（它的 SSE 解析器不接受非流式回收），代理也按流式转发。
        "stream": True,
    }

    if tools:
        chat["tools"] = tools
        tool_choice = payload.get("tool_choice")
        chat["tool_choice"] = tool_choice if isinstance(tool_choice, (str, Mapping)) else "auto"

    # Responses 叫 ``max_output_tokens``，chat 叫 ``max_tokens``。
    max_output = payload.get("max_output_tokens")
    if isinstance(max_output, int) and max_output > 0:
        chat["max_tokens"] = max_output

    for source, target in (("temperature", "temperature"), ("top_p", "top_p")):
        value = payload.get(source)
        if isinstance(value, (int, float)):
            chat[target] = value

    # ``store``/``include``/``prompt_cache_key``/``reasoning`` 是 Responses
    # 专有字段，chat 上游不认识；不透传比传一个它可能 400 的字段更安全。
    return chat


def chat_response_to_responses_body(
    body: Mapping[str, Any], *, model: str = ""
) -> dict[str, Any]:
    """上游 chat 非流式响应体 → Responses 响应体。

    流式路径不经过这里（它逐事件合成）；这个函数服务非流式回收与单元测试。
    """

    choice = (body.get("choices") or [{}])[0]
    choice = choice if isinstance(choice, Mapping) else {}
    message = choice.get("message") if isinstance(choice.get("message"), Mapping) else {}
    output: list[dict[str, Any]] = []

    text = message.get("content")
    if isinstance(text, str) and text:
        output.append(
            {
                "id": "msg_proxy",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        )

    for call in message.get("tool_calls") or []:
        if not isinstance(call, Mapping):
            continue
        function = call.get("function") if isinstance(call.get("function"), Mapping) else {}
        output.append(
            {
                "id": f"fc_{call.get('id') or len(output)}",
                "type": "function_call",
                "status": "completed",
                "call_id": str(call.get("id") or ""),
                "name": str(function.get("name") or ""),
                "arguments": str(function.get("arguments") or "{}"),
            }
        )

    usage = body.get("usage") if isinstance(body.get("usage"), Mapping) else {}
    return {
        "id": str(body.get("id") or "resp_proxy"),
        "object": "response",
        "status": "completed",
        "model": str(body.get("model") or model or ""),
        "output": output,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0)
            or (int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)),
        },
    }


class ResponsesStreamTranslator:
    """把上游 chat SSE 分片翻成 codex 认识的 Responses 事件序列。

    为什么需要状态：Responses 用 ``output_index`` / ``content_index`` 寻址，
    并且每个 output item 有 ``added`` / ``…done`` 一对边界事件。chat 的流
    是「一坨 delta」，要把无序的分片重建出有序的块边界，必须记住「文本块开过
    没有」「每个工具槽位对应哪个 output_index」。

    索引记账是唯一必须精确的地方（与 claude 侧同一个坑）：一个工具调用跟在
    文本之后时必须开在 index 1，而不是 0。codex 按 ``output_index`` 组装
    output 数组，错一位就把工具调用当成文本。
    """

    def __init__(self, *, model: str = "", response_id: str = "resp_proxy") -> None:
        self.model = model
        self.response_id = response_id
        self._next_index = 0
        self._text_index: int | None = None
        self._text_started = False
        self._text_parts: list[str] = []
        self._tool_index_by_slot: dict[int, int] = {}
        self._tool_items: dict[int, dict[str, Any]] = {}
        self._output: list[dict[str, Any]] = []
        self._usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    # -- 生命周期 ---------------------------------------------------------- #

    def start(self) -> list[tuple[str, dict[str, Any]]]:
        """开场的 ``response.created`` / ``response.in_progress``。"""

        return [
            (
                "response.created",
                {
                    "type": "response.created",
                    "response": {
                        "id": self.response_id,
                        "object": "response",
                        "status": "in_progress",
                        "model": self.model,
                        "output": [],
                    },
                },
            ),
            (
                "response.in_progress",
                {
                    "type": "response.in_progress",
                    "response": {"id": self.response_id, "status": "in_progress"},
                },
            ),
        ]

    def push(self, chunk: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        """吃一个 chat SSE chunk，吐零到多条 Responses 事件。"""

        events: list[tuple[str, dict[str, Any]]] = []
        choices = chunk.get("choices") or []
        choice = choices[0] if choices and isinstance(choices[0], Mapping) else {}
        delta = choice.get("delta") if isinstance(choice.get("delta"), Mapping) else {}

        text = delta.get("content")
        if isinstance(text, str) and text:
            events.extend(self._push_text(text))

        for call in delta.get("tool_calls") or []:
            if isinstance(call, Mapping):
                events.extend(self._push_tool_delta(call))

        usage = chunk.get("usage") if isinstance(chunk.get("usage"), Mapping) else {}
        if usage:
            self._usage = {
                "input_tokens": int(usage.get("prompt_tokens") or self._usage["input_tokens"]),
                "output_tokens": int(
                    usage.get("completion_tokens") or self._usage["output_tokens"]
                ),
                "total_tokens": int(usage.get("total_tokens") or 0)
                or (
                    int(usage.get("prompt_tokens") or self._usage["input_tokens"])
                    + int(usage.get("completion_tokens") or self._usage["output_tokens"])
                ),
            }
        return events

    def finish(self) -> list[tuple[str, dict[str, Any]]]:
        """关掉所有打开的块，并以 ``response.completed`` 收尾。

        幂等：重复调用不会重复关闭（开放中的块已经清空）。
        """

        events: list[tuple[str, dict[str, Any]]] = []
        if self._text_started and self._text_index is not None:
            text = "".join(self._text_parts)
            events += [
                (
                    "response.output_text.done",
                    {
                        "type": "response.output_text.done",
                        "item_id": "msg_proxy",
                        "output_index": self._text_index,
                        "content_index": 0,
                        "text": text,
                    },
                ),
                (
                    "response.content_part.done",
                    {
                        "type": "response.content_part.done",
                        "item_id": "msg_proxy",
                        "output_index": self._text_index,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": text, "annotations": []},
                    },
                ),
                (
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "output_index": self._text_index,
                        "item": {
                            "id": "msg_proxy",
                            "type": "message",
                            "role": "assistant",
                            "status": "completed",
                            "content": [
                                {"type": "output_text", "text": text, "annotations": []}
                            ],
                        },
                    },
                ),
            ]
            self._output.append(
                {
                    "id": "msg_proxy",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}],
                }
            )
            self._text_started = False

        for slot, index in sorted(self._tool_index_by_slot.items(), key=lambda kv: kv[1]):
            item = self._tool_items[slot]
            events += [
                (
                    "response.function_call_arguments.done",
                    {
                        "type": "response.function_call_arguments.done",
                        "item_id": item["id"],
                        "output_index": index,
                        "arguments": item["arguments"],
                    },
                ),
                (
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "output_index": index,
                        "item": dict(item),
                    },
                ),
            ]
            self._output.append(dict(item))
        self._tool_index_by_slot.clear()

        events.append(
            (
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {
                        "id": self.response_id,
                        "object": "response",
                        "status": "completed",
                        "model": self.model,
                        "output": list(self._output),
                        "usage": dict(self._usage),
                    },
                },
            )
        )
        return events

    # -- 内部 -------------------------------------------------------------- #

    def _push_text(self, text: str) -> list[tuple[str, dict[str, Any]]]:
        events: list[tuple[str, dict[str, Any]]] = []
        if not self._text_started:
            self._text_started = True
            self._text_index = self._next_index
            self._next_index += 1
            events += [
                (
                    "response.output_item.added",
                    {
                        "type": "response.output_item.added",
                        "output_index": self._text_index,
                        "item": {
                            "id": "msg_proxy",
                            "type": "message",
                            "role": "assistant",
                            "status": "in_progress",
                            "content": [],
                        },
                    },
                ),
                (
                    "response.content_part.added",
                    {
                        "type": "response.content_part.added",
                        "item_id": "msg_proxy",
                        "output_index": self._text_index,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": "", "annotations": []},
                    },
                ),
            ]
        self._text_parts.append(text)
        events.append(
            (
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "item_id": "msg_proxy",
                    "output_index": self._text_index,
                    "content_index": 0,
                    "delta": text,
                },
            )
        )
        return events

    def _push_tool_delta(self, call: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        events: list[tuple[str, dict[str, Any]]] = []
        slot = int(call.get("index") or 0)
        function = call.get("function") if isinstance(call.get("function"), Mapping) else {}

        if slot not in self._tool_index_by_slot:
            # 文本块还开着就先关掉：codex 按 output_index 组装数组，
            # 一个工具块开在文本块之前/之后决定它落在哪个下标。
            # 这里选择「文本先关」，与 Responses 的常见排布一致。
            index = self._next_index
            self._next_index += 1
            self._tool_index_by_slot[slot] = index
            call_id = str(call.get("id") or f"call_{slot}")
            item = {
                "id": f"fc_{index}",
                "type": "function_call",
                "status": "completed",
                "call_id": call_id,
                "name": str(function.get("name") or ""),
                # 参数是流式拼接的 JSON 片段，这里累积，等 finish 时一次性 done。
                "arguments": "",
            }
            self._tool_items[slot] = item
            events.append(
                (
                    "response.output_item.added",
                    {
                        "type": "response.output_item.added",
                        "output_index": index,
                        "item": {**item, "status": "in_progress"},
                    },
                )
            )
        else:
            index = self._tool_index_by_slot[slot]

        item = self._tool_items[slot]
        if function.get("name"):
            item["name"] = str(function["name"])
        if isinstance(call.get("id"), str) and call["id"] and not item["call_id"]:
            item["call_id"] = call["id"]

        arguments = function.get("arguments")
        if isinstance(arguments, str) and arguments:
            item["arguments"] += arguments
            events.append(
                (
                    "response.function_call_arguments.delta",
                    {
                        "type": "response.function_call_arguments.delta",
                        "item_id": item["id"],
                        "output_index": index,
                        "delta": arguments,
                    },
                )
            )
        return events


def responses_request_from_chat_messages(
    messages: Sequence[Mapping[str, Any]], *, model: str = ""
) -> dict[str, Any]:
    """chat 消息 → 最简 Responses 请求体（测试与探针用，生产不经过）。"""

    instructions: list[str] = []
    items: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "user")
        content = _text_of(message.get("content"))
        if role in _SYSTEM_ROLES:
            if content:
                instructions.append(content)
            continue
        items.append(
            {
                "type": "message",
                "role": role,
                "content": [
                    {
                        "type": "output_text" if role == "assistant" else "input_text",
                        "text": content,
                    }
                ],
            }
        )
    body: dict[str, Any] = {"model": model, "input": items}
    if instructions:
        body["instructions"] = "\n\n".join(instructions)
    return body


__all__ = [
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "ResponsesStreamTranslator",
    "chat_response_to_responses_body",
    "responses_request_from_chat_messages",
    "responses_request_to_chat_body",
]