"""Claude Messages 转换器的单元测试。

覆盖 ``app/services/claude_message_converter.py`` 的每一条转换规则:system 抽取、
tool_calls→tool_use、tool→tool_result(且必须在 user 里)、连续同角色合并、
多模态 part、字符串 content 归一化、tools 转换、max_tokens 默认值,以及
arguments 解析失败时的降级行为。
"""

from __future__ import annotations

import json

from app.services.claude_message_converter import (
    DEFAULT_MAX_TOKENS,
    convert_chat_request,
    to_claude_messages,
    to_claude_tools,
)

# ── 规则 1:system 抽取 ──────────────────────────────────────────────────────


def test_system_messages_are_hoisted_to_the_top_level() -> None:
    system, messages = to_claude_messages(
        [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hi"},
        ]
    )
    assert system == "be terse"
    # system 不能再出现在 messages 里——Claude 没有 system role。
    assert messages == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


def test_multiple_system_messages_join_with_a_blank_line() -> None:
    system, _ = to_claude_messages(
        [
            {"role": "system", "content": "rule one"},
            {"role": "system", "content": "rule two"},
            {"role": "user", "content": "hi"},
        ]
    )
    assert system == "rule one\n\nrule two"


def test_an_explicit_system_argument_precedes_extracted_system_messages() -> None:
    system, _ = to_claude_messages(
        [{"role": "system", "content": "from messages"}, {"role": "user", "content": "hi"}],
        system="from argument",
    )
    assert system == "from argument\n\nfrom messages"


def test_the_developer_role_is_treated_as_system() -> None:
    # OpenAI 用 developer 取代 system;两边语义相同,都抽到顶层。
    system, messages = to_claude_messages(
        [{"role": "developer", "content": "dev rule"}, {"role": "user", "content": "hi"}]
    )
    assert system == "dev rule"
    assert len(messages) == 1


def test_convert_omits_the_system_key_when_there_is_none() -> None:
    body = convert_chat_request({"model": "m", "messages": [{"role": "user", "content": "hi"}]})
    assert "system" not in body
    # 返回值为 None 是"没有 system"的契约。
    assert to_claude_messages([{"role": "user", "content": "hi"}])[0] is None


# ── 规则 2:tool_calls → tool_use ────────────────────────────────────────────


def test_assistant_tool_calls_become_tool_use_with_parsed_input() -> None:
    _, messages = to_claude_messages(
        [
            {
                "role": "assistant",
                "content": "let me check",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "foo", "arguments": '{"a": 1, "b": "x"}'},
                    }
                ],
            }
        ]
    )
    assert messages[0]["role"] == "assistant"
    # 文本 block 在 tool_use 之前。
    assert messages[0]["content"][0] == {"type": "text", "text": "let me check"}
    tool_use = messages[0]["content"][1]
    assert tool_use["type"] == "tool_use"
    assert tool_use["id"] == "call_1"
    assert tool_use["name"] == "foo"
    # arguments 是 JSON 字符串,必须被解析成 dict。
    assert tool_use["input"] == {"a": 1, "b": "x"}


def test_an_assistant_with_only_tool_calls_has_no_leading_text_block() -> None:
    _, messages = to_claude_messages(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "foo", "arguments": "{}"}}
                ],
            }
        ]
    )
    assert messages[0]["content"] == [
        {"type": "tool_use", "id": "c1", "name": "foo", "input": {}}
    ]


def test_empty_arguments_string_becomes_an_empty_input_object() -> None:
    _, messages = to_claude_messages(
        [{"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "n", "arguments": ""}}]}]
    )
    assert messages[0]["content"][0]["input"] == {}


def test_a_nameless_tool_call_is_dropped() -> None:
    _, messages = to_claude_messages(
        [
            {
                "role": "assistant",
                "content": "text",
                "tool_calls": [{"id": "c1", "function": {"name": "", "arguments": "{}"}}],
            }
        ]
    )
    assert messages[0]["content"] == [{"type": "text", "text": "text"}]


# ── 规则 2 的降级:arguments JSON 解析失败 ───────────────────────────────────


def test_unparseable_arguments_degrade_instead_of_raising() -> None:
    truncated = '{"a": 1, "b":'
    _, messages = to_claude_messages(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "foo", "arguments": truncated}}
                ],
            }
        ]
    )
    tool_use = messages[0]["content"][0]
    # 不抛异常,原始字符串与原因都保留下来。
    assert tool_use["input"]["_raw"] == truncated
    assert "_parse_error" in tool_use["input"]
    assert tool_use["name"] == "foo"


def test_arguments_json_that_is_not_an_object_also_degrades() -> None:
    _, messages = to_claude_messages(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "c1", "function": {"name": "foo", "arguments": '"just a string"'}}
                ],
            }
        ]
    )
    assert messages[0]["content"][0]["input"]["_raw"] == '"just a string"'


# ── 规则 3:tool 角色 → user 里的 tool_result ────────────────────────────────


def test_tool_messages_become_tool_results_inside_a_user_message() -> None:
    _, messages = to_claude_messages(
        [
            {"role": "user", "content": "run it"},
            {
                "role": "assistant",
                "tool_calls": [{"id": "call_1", "function": {"name": "foo", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "the result"},
        ]
    )
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    tool_result = messages[-1]["content"][0]
    assert tool_result == {
        "type": "tool_result",
        "tool_use_id": "call_1",
        "content": "the result",
    }


def test_consecutive_tool_results_merge_into_one_user_message() -> None:
    # 关键规则:两条 tool 结果必须并进同一条 user,否则会出现 user,user 连续。
    _, messages = to_claude_messages(
        [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "c1", "function": {"name": "foo", "arguments": "{}"}},
                    {"id": "c2", "function": {"name": "bar", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "r1"},
            {"role": "tool", "tool_call_id": "c2", "content": "r2"},
        ]
    )
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    results = messages[-1]["content"]
    assert len(results) == 2
    assert results[0]["tool_use_id"] == "c1"
    assert results[1]["tool_use_id"] == "c2"


def test_a_trailing_user_turn_merges_with_the_tool_result_turn() -> None:
    # 一次工具往返后用户又发了消息:两条 user 必须合成一条。
    _, messages = to_claude_messages(
        [
            {
                "role": "assistant",
                "tool_calls": [{"id": "c1", "function": {"name": "foo", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "r1"},
            {"role": "user", "content": "next please"},
        ]
    )
    assert [m["role"] for m in messages] == ["assistant", "user"]
    kinds = [block["type"] for block in messages[-1]["content"]]
    assert kinds == ["tool_result", "text"]


def test_a_tool_result_with_no_matching_call_is_dropped() -> None:
    """A result that names no existing call is dropped, not passed through.

    Claude rejects a ``tool_use_id`` it cannot resolve, so forwarding an orphan
    result would fail the whole request. An earlier version emitted it with an
    empty id, which had exactly that effect.
    """

    _, messages = to_claude_messages([{"role": "tool", "content": "orphan"}])
    assert messages == []


# ── 规则 4:连续同角色合并 ──────────────────────────────────────────────────


def test_two_consecutive_user_messages_merge_into_one() -> None:
    _, messages = to_claude_messages(
        [
            {"role": "user", "content": "first"},
            {"role": "user", "content": "second"},
        ]
    )
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == [
        {"type": "text", "text": "first"},
        {"type": "text", "text": "second"},
    ]


def test_two_consecutive_assistant_messages_merge_into_one() -> None:
    _, messages = to_claude_messages(
        [
            {"role": "assistant", "content": "one"},
            {"role": "assistant", "content": "two"},
        ]
    )
    assert len(messages) == 1
    assert messages[0]["content"] == [
        {"type": "text", "text": "one"},
        {"type": "text", "text": "two"},
    ]


def test_roles_are_strictly_alternating_in_a_full_transcript() -> None:
    _, messages = to_claude_messages(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u1"},
            {"role": "user", "content": "u2"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c1", "function": {"name": "f", "arguments": "{}"}},
                    {"id": "c2", "function": {"name": "g", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "r1"},
            {"role": "tool", "tool_call_id": "c2", "content": "r2"},
            {"role": "assistant", "content": "done"},
        ]
    )
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert all(a != b for a, b in zip(roles, roles[1:]))


# ── 规则 5:多模态 content ───────────────────────────────────────────────────


def test_data_url_image_is_split_into_media_type_and_base64() -> None:
    _, messages = to_claude_messages(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAABBBCCC=="},
                    },
                ],
            }
        ]
    )
    blocks = messages[0]["content"]
    assert blocks[0] == {"type": "text", "text": "look"}
    assert blocks[1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "AAABBBCCC=="},
    }


def test_data_url_base64_strips_embedded_whitespace() -> None:
    # 有些客户端会把 base64 折行,空白必须去掉再交给 Claude。
    _, messages = to_claude_messages(
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64,AA\nBB\tCC"},
                    }
                ],
            }
        ]
    )
    source = messages[0]["content"][0]["source"]
    assert source["media_type"] == "image/jpeg"
    assert source["data"] == "AABBCC"


def test_remote_image_url_becomes_a_url_source() -> None:
    _, messages = to_claude_messages(
        [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "https://example.test/pic.png"}}
                ],
            }
        ]
    )
    assert messages[0]["content"][0] == {
        "type": "image",
        "source": {"type": "url", "url": "https://example.test/pic.png"},
    }


def test_a_non_image_data_url_degrades_to_a_placeholder() -> None:
    """An unusable image becomes a text placeholder rather than disappearing.

    Dropping it would keep the request valid but hide the attachment from the
    model entirely, so a solver would retry a dead end with no idea an image had
    ever existed. The placeholder is information it can act on.
    """

    _, messages = to_claude_messages(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "keep"},
                    {"type": "image_url", "image_url": {"url": "data:text/plain;base64,AA"}},
                ],
            }
        ]
    )
    assert messages[0]["content"] == [
        {"type": "text", "text": "keep"},
        {"type": "text", "text": "[image omitted: unreadable image_url]"},
    ]


def test_unknown_content_part_types_are_dropped() -> None:
    _, messages = to_claude_messages(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "keep"},
                    {"type": "audio", "audio": {"data": "xx"}},
                ],
            }
        ]
    )
    assert messages[0]["content"] == [{"type": "text", "text": "keep"}]


# ── 规则 6:字符串 content 归一化 / 空值处理 ─────────────────────────────────


def test_plain_string_content_is_wrapped_in_a_text_block() -> None:
    _, messages = to_claude_messages([{"role": "user", "content": "hello"}])
    assert messages == [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]


def test_empty_string_content_never_produces_an_empty_content_array() -> None:
    # Claude 不接受 content: [];整条消息应被跳过。
    _, messages = to_claude_messages(
        [
            {"role": "user", "content": ""},
            {"role": "assistant", "content": None},
            {"role": "user", "content": "real"},
        ]
    )
    assert messages == [{"role": "user", "content": [{"type": "text", "text": "real"}]}]


def test_an_assistant_with_no_content_and_no_tool_calls_is_dropped() -> None:
    _, messages = to_claude_messages(
        [{"role": "assistant", "content": None}, {"role": "user", "content": "hi"}]
    )
    assert messages == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


def test_every_claude_message_has_a_non_empty_content_list() -> None:
    _, messages = to_claude_messages(
        [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": ""},
        ]
    )
    assert messages
    assert all(message["content"] for message in messages)


# ── 规则 7:tools 转换 ──────────────────────────────────────────────────────


def test_openai_tools_become_claude_input_schema_tools() -> None:
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}}
    tools = to_claude_tools(
        [
            {
                "type": "function",
                "function": {"name": "foo", "description": "does foo", "parameters": schema},
            }
        ]
    )
    assert tools == [{"name": "foo", "description": "does foo", "input_schema": schema}]


def test_a_tool_without_parameters_gets_an_empty_object_schema() -> None:
    tools = to_claude_tools([{"type": "function", "function": {"name": "foo"}}])
    assert tools[0]["input_schema"] == {"type": "object", "properties": {}}
    # 没有 description 时不写这个键。
    assert "description" not in tools[0]


def test_convert_omits_the_tools_key_when_there_are_no_tools() -> None:
    body = convert_chat_request({"model": "m", "messages": [{"role": "user", "content": "hi"}]})
    assert "tools" not in body
    assert convert_chat_request(
        {"model": "m", "messages": [], "tools": []}
    ).get("tools") is None


# ── 规则 8:顶层参数 model / max_tokens ─────────────────────────────────────


def test_model_is_passed_through() -> None:
    body = convert_chat_request(
        {"model": "claude-3-5-sonnet", "messages": [{"role": "user", "content": "hi"}]}
    )
    assert body["model"] == "claude-3-5-sonnet"


def test_max_tokens_defaults_when_the_caller_omits_it() -> None:
    body = convert_chat_request({"model": "m", "messages": [{"role": "user", "content": "hi"}]})
    assert body["max_tokens"] == DEFAULT_MAX_TOKENS == 4096


def test_max_tokens_survives_when_the_caller_supplies_it() -> None:
    body = convert_chat_request(
        {"model": "m", "messages": [], "max_tokens": 128}
    )
    assert body["max_tokens"] == 128


def test_an_invalid_max_tokens_falls_back_to_the_default() -> None:
    for bad in (None, 0, -5, "many", True):
        body = convert_chat_request({"model": "m", "messages": [], "max_tokens": bad})
        assert body["max_tokens"] == DEFAULT_MAX_TOKENS


def test_a_custom_default_max_tokens_is_honoured() -> None:
    body = convert_chat_request({"model": "m", "messages": []}, default_max_tokens=1024)
    assert body["max_tokens"] == 1024


# ── 端到端:完整往返 ────────────────────────────────────────────────────────


def test_a_full_openai_request_round_trips_into_a_claude_request() -> None:
    payload = {
        "model": "claude-3-5-sonnet",
        "messages": [
            {"role": "system", "content": "You are a CTF agent."},
            {"role": "user", "content": "check the target"},
            {
                "role": "assistant",
                "content": "running a request",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "http_request", "arguments": '{"method":"GET","url":"/x"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": json.dumps({"status_code": 200})},
            {"role": "assistant", "content": "target is up"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "http_request",
                    "description": "do a request",
                    "parameters": {"type": "object", "properties": {"url": {"type": "string"}}},
                },
            }
        ],
    }
    body = convert_chat_request(payload)
    assert body["model"] == "claude-3-5-sonnet"
    assert body["max_tokens"] == DEFAULT_MAX_TOKENS
    assert body["system"] == "You are a CTF agent."
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user", "assistant"]
    # 工具调用与结果通过 id 关联。
    tool_use = body["messages"][1]["content"][1]
    tool_result = body["messages"][2]["content"][0]
    assert tool_use["type"] == "tool_use" and tool_use["id"] == "call_1"
    assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "call_1"
    assert body["tools"][0]["input_schema"]["properties"] == {"url": {"type": "string"}}


def test_tolerates_non_mapping_entries_and_a_missing_messages_key() -> None:
    body = convert_chat_request({"model": "m"})
    assert body["messages"] == []
    _, messages = to_claude_messages([None, "junk", {"role": "user", "content": "hi"}])  # type: ignore[list-item]
    assert messages == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


def test_converter_is_pure_and_does_not_mutate_its_input() -> None:
    tool_calls = [
        {"id": "c1", "function": {"name": "foo", "arguments": '{"a": 1}'}}
    ]
    messages = [{"role": "assistant", "content": "x", "tool_calls": tool_calls}]
    snapshot = json.dumps(messages, sort_keys=True)
    to_claude_messages(messages)
    assert json.dumps(messages, sort_keys=True) == snapshot


# ── tool_use ↔ tool_result 配对不变量 ────────────────────────────────────────
# Claude 要求两者严格一一对应，破坏它会让整个请求 400。这两条守住的正是
# 「丢弃畸形块」这个修复曾经制造出的 bug。


def _pairing(messages: list[dict]) -> tuple[set[str], set[str]]:
    calls = {b["id"] for m in messages for b in m["content"] if b.get("type") == "tool_use"}
    results = {b["tool_use_id"] for m in messages for b in m["content"] if b.get("type") == "tool_result"}
    return calls, results


def test_a_call_with_no_result_gets_a_synthetic_result() -> None:
    """无结果的调用要补一条结果，而不是丢弃这个调用。

    丢弃会让模型忘记自己调用过工具，于是重复调用同一个工具——这正是图像
    占位符要防止的那类失败。
    """

    _, messages = to_claude_messages(
        [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "foo", "arguments": "{}"}}],
            },
        ]
    )
    calls, results = _pairing(messages)
    assert calls == results == {"c1"}, "调用必须有配对结果"
    synthetic = [b for m in messages for b in m["content"] if b.get("type") == "tool_result"]
    assert synthetic[0]["is_error"] is True
    assert "No tool result" in synthetic[0]["content"]


def test_a_malformed_call_does_not_orphan_its_result() -> None:
    """无 name 的畸形调用被丢弃时，它的结果也必须一并处理，不能留下孤儿引用。

    这是本次修复的核心：旧实现丢掉畸形 tool_use 却留下 tool_result，
    为了「避免 400」反而制造了 400。
    """

    _, messages = to_claude_messages(
        [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "result of c1"},
        ]
    )
    calls, results = _pairing(messages)
    assert results <= calls, f"存在孤儿 tool_result: {results - calls}"


def test_serialized_history_never_has_an_orphan_half() -> None:
    """回归护栏：任意组合下都不产出孤儿引用。"""

    payload = {
        "model": "m",
        "messages": [
            {"role": "user", "content": "go"},
            # 畸形调用（无 name）＋ 它的结果 → 两侧都要清理
            {"role": "assistant", "content": "", "tool_calls": [{"id": "bad", "function": {"arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "bad", "content": "r"},
            # 正常调用但结果缺失 → 要补合成结果
            {"role": "assistant", "content": "", "tool_calls": [{"id": "ok", "function": {"name": "foo", "arguments": "{}"}}]},
            {"role": "user", "content": "carry on"},
            # 历史被截断，只留下结果 → 丢弃
            {"role": "tool", "tool_call_id": "ghost", "content": "r"},
        ],
    }
    body = convert_chat_request(payload)
    calls, results = _pairing(body["messages"])
    assert results <= calls, f"孤儿 tool_result: {results - calls}"
    assert calls <= results, f"无结果的 tool_use: {calls - results}"


def test_adjacent_same_role_is_merged_in_the_final_output() -> None:
    """合并发生在最终输出上，包括配对修复新插入的 user 消息之后。"""

    _, messages = to_claude_messages(
        [
            {"role": "user", "content": "a"},
            {"role": "user", "content": "b"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}]},
        ]
    )
    roles = [m["role"] for m in messages]
    assert all(roles[i] != roles[i + 1] for i in range(len(roles) - 1)), f"存在连续同角色: {roles}"


# ── validate_claude_request：在边界上暴露问题 ────────────────────────────────
# 转换器可以产出不可发送的 body（例如输入里没有可发的对话轮次）。静默产出会让
# 错误在远端以无上下文的 HTTP 400 出现——和孤儿 tool_result 是同一类沉默失败。


def test_a_converted_valid_request_reports_no_problems() -> None:
    from app.services.claude_message_converter import validate_claude_request

    body = convert_chat_request(
        {
            "model": "m",
            "messages": [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": "calling",
                    "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}],
                },
                {"role": "tool", "tool_call_id": "c1", "content": "r"},
            ],
        }
    )
    assert validate_claude_request(body) == []


def test_an_empty_message_list_is_reported_not_silently_returned() -> None:
    """空 messages 会被 Anthropic 拒绝，校验函数要让调用方提前知道。"""

    from app.services.claude_message_converter import validate_claude_request

    body = convert_chat_request({"model": "m", "messages": []})
    problems = validate_claude_request(body)
    assert problems and "at least one turn" in problems[0]


def test_a_system_only_input_is_reported_as_having_no_turns() -> None:
    from app.services.claude_message_converter import validate_claude_request

    body = convert_chat_request({"model": "m", "messages": [{"role": "system", "content": "s"}]})
    assert body["messages"] == []
    assert validate_claude_request(body)


def test_the_validator_catches_a_hand_built_orphan_result() -> None:
    """校验函数独立于转换器：手工构造的坏 body 也要能查出来。"""

    from app.services.claude_message_converter import validate_claude_request

    body = {
        "max_tokens": 16,
        "messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "ghost", "content": "x"}]}],
    }
    problems = validate_claude_request(body)
    assert any("ghost" in problem for problem in problems)


def test_the_validator_reports_consecutive_same_role_and_bad_max_tokens() -> None:
    from app.services.claude_message_converter import validate_claude_request

    body = {
        "max_tokens": 0,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "a"}]},
            {"role": "user", "content": [{"type": "text", "text": "b"}]},
        ],
    }
    problems = validate_claude_request(body)
    assert any("share role" in problem for problem in problems)
    assert any("max_tokens" in problem for problem in problems)


# ── 反向：Claude → chat（cc-switch 式代理的下行方向）────────────────────────


def test_claude_system_becomes_a_leading_system_message() -> None:
    from app.services.claude_message_converter import claude_request_to_chat_body

    body = claude_request_to_chat_body(
        {"model": "m", "system": "be terse", "messages": [{"role": "user", "content": "hi"}]}
    )
    assert body["messages"][0] == {"role": "system", "content": "be terse"}
    assert body["messages"][1] == {"role": "user", "content": "hi"}


def test_claude_tool_use_becomes_tool_calls_with_string_arguments() -> None:
    from app.services.claude_message_converter import claude_request_to_chat_body

    body = claude_request_to_chat_body(
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "calling"},
                        {"type": "tool_use", "id": "t1", "name": "f", "input": {"a": 1}},
                    ],
                }
            ]
        }
    )
    message = body["messages"][0]
    assert message["content"] == "calling"
    call = message["tool_calls"][0]
    assert call["id"] == "t1"
    assert call["function"]["name"] == "f"
    # chat 的 arguments 是 JSON 文本，不是对象。
    assert isinstance(call["function"]["arguments"], str)
    assert json.loads(call["function"]["arguments"]) == {"a": 1}


def test_one_claude_user_message_expands_into_tool_messages() -> None:
    """Claude 把多个 tool_result 放在一条 user 里；chat 要求每条一个 tool 消息。"""

    from app.services.claude_message_converter import claude_request_to_chat_body

    body = claude_request_to_chat_body(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "a", "content": "ra"},
                        {"type": "tool_result", "tool_use_id": "b", "content": "rb"},
                    ],
                }
            ]
        }
    )
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["tool", "tool"]
    assert [m["tool_call_id"] for m in body["messages"]] == ["a", "b"]


def test_claude_image_base64_is_rebuilt_into_a_data_url() -> None:
    from app.services.claude_message_converter import claude_request_to_chat_body

    body = claude_request_to_chat_body(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "see"},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA"}},
                    ],
                }
            ]
        }
    )
    parts = body["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "see"}
    assert parts[1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}


def test_claude_url_image_passes_through_as_image_url() -> None:
    from app.services.claude_message_converter import claude_request_to_chat_body

    body = claude_request_to_chat_body(
        {"messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}}]}]}
    )
    assert body["messages"][0]["content"][0]["image_url"]["url"] == "https://x/y.png"


def test_stop_sequences_is_renamed_to_stop() -> None:
    from app.services.claude_message_converter import claude_request_to_chat_body

    body = claude_request_to_chat_body(
        {"messages": [{"role": "user", "content": "x"}], "stop_sequences": ["END"]}
    )
    assert body["stop"] == ["END"]
    assert "stop_sequences" not in body


def test_claude_tool_choice_is_mapped_not_passed_through() -> None:
    """Claude 的 any/tool 在 chat 里没有同名字面量，必须映射。"""

    from app.services.claude_message_converter import claude_request_to_chat_body

    def choice(value):
        return claude_request_to_chat_body(
            {"messages": [{"role": "user", "content": "x"}], "tool_choice": value}
        ).get("tool_choice")

    assert choice({"type": "auto"}) == "auto"
    assert choice({"type": "any"}) == "required"
    assert choice({"type": "tool", "name": "f"}) == {"type": "function", "function": {"name": "f"}}
    assert choice({"type": "none"}) == "none"


def test_claude_tools_use_input_schema_as_parameters() -> None:
    from app.services.claude_message_converter import claude_request_to_chat_body

    body = claude_request_to_chat_body(
        {"messages": [{"role": "user", "content": "x"}],
         "tools": [{"name": "f", "description": "d", "input_schema": {"type": "object"}}]}
    )
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["function"]["parameters"] == {"type": "object"}


def test_a_text_only_user_message_stays_a_plain_string() -> None:
    """纯文本走字符串而非 parts 数组：更多兼容中转支持这种形状。"""

    from app.services.claude_message_converter import claude_request_to_chat_body

    body = claude_request_to_chat_body(
        {"messages": [{"role": "user", "content": [
            {"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}]}
    )
    assert body["messages"][0]["content"] == "a\nb"


# ── 响应升回：chat → Claude ─────────────────────────────────────────────────


def test_chat_response_becomes_claude_message_with_usage_mapping() -> None:
    from app.services.claude_message_converter import chat_response_to_claude_body

    body = chat_response_to_claude_body(
        {
            "id": "c1",
            "model": "deepseek",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "hello"}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        }
    )
    assert body["type"] == "message" and body["role"] == "assistant"
    assert body["content"] == [{"type": "text", "text": "hello"}]
    assert body["stop_reason"] == "end_turn"
    assert body["usage"] == {"input_tokens": 11, "output_tokens": 7}


def test_finish_reason_tool_calls_becomes_stop_reason_tool_use() -> None:
    """下游靠 stop_reason 判断该去执行工具；映射错了 agent 循环就断了。"""

    from app.services.claude_message_converter import chat_response_to_claude_body

    body = chat_response_to_claude_body(
        {
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "t1", "function": {"name": "f", "arguments": '{"a":1}'}}]}}],
        }
    )
    assert body["stop_reason"] == "tool_use"
    assert body["content"][0]["type"] == "tool_use"
    # Claude 侧 input 是对象，且上游给的 JSON 文本要解析。
    assert body["content"][0]["input"] == {"a": 1}


def test_length_finish_reason_becomes_max_tokens() -> None:
    from app.services.claude_message_converter import chat_response_to_claude_body

    body = chat_response_to_claude_body(
        {"choices": [{"finish_reason": "length", "message": {"role": "assistant", "content": "x"}}]}
    )
    assert body["stop_reason"] == "max_tokens"


def test_an_empty_chat_message_still_yields_a_non_empty_content_list() -> None:
    """Claude 不接受空 content 数组；给空文本 block 比给空数组安全。"""

    from app.services.claude_message_converter import chat_response_to_claude_body

    body = chat_response_to_claude_body(
        {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": None}}]}
    )
    assert body["content"] == [{"type": "text", "text": ""}]


def test_a_malformed_tool_arguments_in_a_response_degrades_not_raises() -> None:
    from app.services.claude_message_converter import chat_response_to_claude_body

    body = chat_response_to_claude_body(
        {"choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "t", "function": {"name": "f", "arguments": "{broken"}}]}}]}
    )
    block = body["content"][0]
    assert block["type"] == "tool_use"
    assert "_parse_error" in block["input"]
    assert block["input"]["_raw"] == "{broken"
