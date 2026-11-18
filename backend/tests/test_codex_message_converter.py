"""回归：Responses ↔ chat 转换器（codex 本地代理的翻译层）。

这些测试锁住的是「codex 0.148 实际发出来的形状」，不是文档形状。每一条
对应的都是一次实测：codex 的请求体是抓真的 CLI 得到的，不是照文档写的。

最关键的一组是工具声明——实测 codex 会**两处**声明工具（顶层扁平 ``tools``
与 ``input[].additional_tools`` 的 namespace 嵌套），只处理一处的后果不是报错
而是静默退化：模型把工具调用当正文吐出来。所以两种形状都必须有用例。
"""

from __future__ import annotations

import json

from app.services.codex_message_converter import (
    ResponsesStreamTranslator,
    chat_response_to_responses_body,
    responses_request_to_chat_body,
)


def _events_of(events: list[tuple[str, dict]]) -> list[str]:
    return [name for name, _ in events]


# -- 请求方向：Responses -> chat ------------------------------------------- #


def test_instructions_become_a_leading_system_message() -> None:
    chat = responses_request_to_chat_body(
        {"model": "m", "instructions": "SYS", "input": []}
    )
    assert chat["messages"][0] == {"role": "system", "content": "SYS"}


def test_the_developer_role_is_downgraded_to_system() -> None:
    # Real relays reject it outright:
    #   developer is not one of ['system', 'assistant', 'user', 'tool', 'function']
    chat = responses_request_to_chat_body(
        {
            "input": [
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "be terse"}],
                }
            ]
        }
    )
    assert chat["messages"] == [{"role": "system", "content": "be terse"}]


def test_consecutive_developer_messages_are_coalesced() -> None:
    # codex emits several developer turns (base prompt, skills, permissions).
    # Some relays reject a repeated system role the same way they reject
    # ``developer``, so they are joined rather than forwarded verbatim.
    chat = responses_request_to_chat_body(
        {
            "instructions": "BASE",
            "input": [
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "SECOND"}],
                },
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "THIRD"}],
                },
            ],
        }
    )
    assert len(chat["messages"]) == 1
    assert chat["messages"][0]["content"] == "BASE\n\nSECOND\n\nTHIRD"


def test_user_messages_are_not_merged() -> None:
    # Only system turns are coalesced; merging user turns would change meaning.
    chat = responses_request_to_chat_body(
        {
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "a"}]},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "b"}]},
            ]
        }
    )
    assert [m["content"] for m in chat["messages"]] == ["a", "b"]


def test_top_level_flat_tools_are_forwarded() -> None:
    # Measured on a real codex run: the flat Responses shape.
    chat = responses_request_to_chat_body(
        {
            "tools": [
                {
                    "type": "function",
                    "name": "shell",
                    "description": "run",
                    "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
                }
            ],
            "input": [],
        }
    )
    assert chat["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "shell",
                "description": "run",
                "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
            },
        }
    ]
    assert chat["tool_choice"] == "auto"


def test_additional_tools_namespaces_are_flattened() -> None:
    # Also measured: 0.148 multi-agent mode nests them under namespaces.
    chat = responses_request_to_chat_body(
        {
            "input": [
                {
                    "type": "additional_tools",
                    "role": "developer",
                    "tools": [
                        {
                            "type": "namespace",
                            "name": "collaboration",
                            "tools": [
                                {
                                    "type": "function",
                                    "name": "spawn_agent",
                                    "description": "spawn",
                                    "parameters": {
                                        "type": "object",
                                        "properties": {"task_name": {"type": "string"}},
                                        "required": ["task_name"],
                                    },
                                }
                            ],
                        }
                    ],
                },
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]},
            ]
        }
    )
    assert [t["function"]["name"] for t in chat["tools"]] == ["spawn_agent"]


def test_custom_freeform_tools_become_a_single_string_argument() -> None:
    # codex's ``exec`` tool is a custom (grammar-constrained) tool. chat has no
    # equivalent, so it is folded into one string argument rather than dropped --
    # losing it would silently remove the ability to run anything.
    chat = responses_request_to_chat_body(
        {
            "input": [
                {
                    "type": "additional_tools",
                    "role": "developer",
                    "tools": [
                        {
                            "type": "namespace",
                            "name": "functions",
                            "tools": [
                                {
                                    "type": "custom",
                                    "name": "exec",
                                    "description": "run js",
                                    "format": {"type": "grammar", "syntax": "lark", "definition": "x"},
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    )
    tool = chat["tools"][0]["function"]
    assert tool["name"] == "exec"
    assert tool["parameters"]["properties"]["input"]["type"] == "string"
    assert tool["parameters"]["required"] == ["input"]


def test_a_tool_without_parameters_gets_an_empty_object_schema() -> None:
    chat = responses_request_to_chat_body(
        {"tools": [{"type": "function", "name": "noop"}], "input": []}
    )
    assert chat["tools"][0]["function"]["parameters"] == {"type": "object", "properties": {}}


def test_no_tools_means_no_tools_key() -> None:
    chat = responses_request_to_chat_body({"input": []})
    assert "tools" not in chat


def test_a_previous_function_call_becomes_an_assistant_tool_call() -> None:
    chat = responses_request_to_chat_body(
        {
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "shell",
                    "arguments": '{"command": "ls"}',
                }
            ]
        }
    )
    message = chat["messages"][0]
    assert message["role"] == "assistant"
    assert message["tool_calls"][0]["id"] == "call_1"
    assert message["tool_calls"][0]["function"]["arguments"] == '{"command": "ls"}'


def test_a_function_call_output_becomes_a_tool_message() -> None:
    # The call_id must survive: codex matches the result to the call by it, and
    # losing it breaks every turn after the first tool use.
    chat = responses_request_to_chat_body(
        {
            "input": [
                {"type": "function_call_output", "call_id": "call_1", "output": "done"}
            ]
        }
    )
    assert chat["messages"][0] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "done",
    }


def test_a_full_tool_round_trip_keeps_the_call_id() -> None:
    chat = responses_request_to_chat_body(
        {
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "ls"}]},
                {"type": "function_call", "call_id": "call_9", "name": "shell", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "call_9", "output": "files"},
            ]
        }
    )
    assert [m["role"] for m in chat["messages"]] == ["user", "assistant", "tool"]
    assert chat["messages"][1]["tool_calls"][0]["id"] == "call_9"
    assert chat["messages"][2]["tool_call_id"] == "call_9"


def test_max_output_tokens_maps_to_max_tokens() -> None:
    chat = responses_request_to_chat_body({"input": [], "max_output_tokens": 512})
    assert chat["max_tokens"] == 512


def test_an_absent_max_output_tokens_is_not_invented() -> None:
    chat = responses_request_to_chat_body({"input": []})
    assert "max_tokens" not in chat


def test_sampling_parameters_are_passed_through() -> None:
    chat = responses_request_to_chat_body({"input": [], "temperature": 0.3, "top_p": 0.9})
    assert (chat["temperature"], chat["top_p"]) == (0.3, 0.9)


def test_responses_only_fields_are_not_forwarded() -> None:
    # ``reasoning`` / ``store`` / ``include`` / ``prompt_cache_key`` mean nothing
    # to a chat relay; forwarding them invites a 400 on an unknown field.
    chat = responses_request_to_chat_body(
        {
            "input": [],
            "reasoning": {"effort": "high"},
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "prompt_cache_key": "abc",
        }
    )
    for key in ("reasoning", "store", "include", "prompt_cache_key"):
        assert key not in chat


def test_unknown_input_items_are_skipped_not_fatal() -> None:
    chat = responses_request_to_chat_body(
        {
            "input": [
                {"type": "reasoning", "summary": []},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            ]
        }
    )
    assert chat["messages"] == [{"role": "user", "content": "hi"}]


def test_streaming_is_always_requested() -> None:
    # codex's SSE parser never accepts a buffered reply, so the proxy asks the
    # upstream to stream unconditionally.
    assert responses_request_to_chat_body({"input": []})["stream"] is True


def test_model_falls_back_to_the_default_when_absent() -> None:
    chat = responses_request_to_chat_body({"input": []}, default_model="fallback")
    assert chat["model"] == "fallback"


# -- 响应方向：chat -> Responses ------------------------------------------- #


def test_a_chat_reply_becomes_a_responses_message_item() -> None:
    body = chat_response_to_responses_body(
        {
            "id": "chatcmpl-1",
            "model": "m",
            "choices": [{"message": {"role": "assistant", "content": "hello"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        }
    )
    assert body["object"] == "response"
    assert body["status"] == "completed"
    assert body["output"][0]["type"] == "message"
    assert body["output"][0]["content"][0]["text"] == "hello"
    assert body["usage"] == {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7}


def test_a_chat_tool_call_becomes_a_responses_function_call_item() -> None:
    body = chat_response_to_responses_body(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_7",
                                "type": "function",
                                "function": {"name": "shell", "arguments": '{"a":1}'},
                            }
                        ],
                    }
                }
            ]
        }
    )
    item = body["output"][0]
    assert item["type"] == "function_call"
    assert item["call_id"] == "call_7"
    assert item["name"] == "shell"
    assert json.loads(item["arguments"]) == {"a": 1}


def test_usage_totals_are_derived_when_the_upstream_omits_them() -> None:
    body = chat_response_to_responses_body(
        {"choices": [{"message": {"content": "x"}}], "usage": {"prompt_tokens": 2, "completion_tokens": 5}}
    )
    assert body["usage"]["total_tokens"] == 7


def test_an_empty_chat_reply_still_produces_a_well_formed_body() -> None:
    body = chat_response_to_responses_body({})
    assert body["status"] == "completed"
    assert body["output"] == []


# -- 流式翻译 --------------------------------------------------------------- #


def _collect(chunks: list[dict]) -> list[tuple[str, dict]]:
    translator = ResponsesStreamTranslator(model="m")
    events = list(translator.start())
    for chunk in chunks:
        events.extend(translator.push(chunk))
    events.extend(translator.finish())
    return events


def test_a_text_stream_is_bracketed_by_the_expected_events() -> None:
    events = _collect(
        [
            {"choices": [{"delta": {"content": "he"}}]},
            {"choices": [{"delta": {"content": "llo"}}]},
        ]
    )
    names = _events_of(events)
    assert names[0] == "response.created"
    assert names[1] == "response.in_progress"
    assert "response.output_text.delta" in names
    assert names[-1] == "response.completed"


def test_text_deltas_are_the_sum_of_the_incoming_chunks() -> None:
    events = _collect(
        [
            {"choices": [{"delta": {"content": "he"}}]},
            {"choices": [{"delta": {"content": "llo"}}]},
        ]
    )
    deltas = [d["delta"] for n, d in events if n == "response.output_text.delta"]
    assert "".join(deltas) == "hello"


def test_the_text_block_opens_once_and_closes_once() -> None:
    events = _collect(
        [
            {"choices": [{"delta": {"content": "a"}}]},
            {"choices": [{"delta": {"content": "b"}}]},
            {"choices": [{"delta": {"content": "c"}}]},
        ]
    )
    names = _events_of(events)
    assert names.count("response.output_item.added") == 1
    assert names.count("response.content_part.added") == 1
    assert names.count("response.output_item.done") == 1


def test_a_tool_call_opens_at_the_next_index_after_text() -> None:
    # Index accounting is the part that must be exact: codex assembles its
    # output array by output_index, so a tool call following text has to open at
    # index 1, not 0 -- getting this wrong makes codex read it as prose.
    events = _collect(
        [
            {"choices": [{"delta": {"content": "thinking"}}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "call_1", "function": {"name": "shell", "arguments": '{"a"'}}
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ":1}"}}]}}
                ]
            },
        ]
    )
    added = [d["output_index"] for n, d in events if n == "response.output_item.added"]
    assert added == [0, 1]


def test_tool_arguments_accumulate_across_chunks() -> None:
    events = _collect(
        [
            {
                "choices": [
                    {"delta": {"tool_calls": [{"index": 0, "id": "c", "function": {"name": "shell", "arguments": '{"cmd"'}}]}}
                ]
            },
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ': "ls"}'}}]}}]},
        ]
    )
    done = [d for n, d in events if n == "response.function_call_arguments.done"]
    assert done[0]["arguments"] == '{"cmd": "ls"}'


def test_a_tool_call_survives_into_the_completed_output() -> None:
    events = _collect(
        [
            {
                "choices": [
                    {"delta": {"tool_calls": [{"index": 0, "id": "call_5", "function": {"name": "shell", "arguments": "{}"}}]}}
                ]
            }
        ]
    )
    completed = [d for n, d in events if n == "response.completed"][0]
    item = completed["response"]["output"][0]
    assert item["type"] == "function_call"
    assert item["call_id"] == "call_5"


def test_two_tools_get_two_distinct_indices() -> None:
    events = _collect(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "a", "function": {"name": "t1", "arguments": "{}"}},
                                {"index": 1, "id": "b", "function": {"name": "t2", "arguments": "{}"}},
                            ]
                        }
                    }
                ]
            }
        ]
    )
    added = [d["output_index"] for n, d in events if n == "response.output_item.added"]
    assert added == [0, 1]
    assert _events_of(events).count("response.output_item.done") == 2


def test_a_tool_only_stream_has_no_message_item() -> None:
    events = _collect(
        [
            {
                "choices": [
                    {"delta": {"tool_calls": [{"index": 0, "id": "c", "function": {"name": "t", "arguments": "{}"}}]}}
                ]
            }
        ]
    )
    completed = [d for n, d in events if n == "response.completed"][0]
    assert [i["type"] for i in completed["response"]["output"]] == ["function_call"]


def test_usage_from_the_upstream_is_carried_into_the_completed_event() -> None:
    events = _collect(
        [
            {"choices": [{"delta": {"content": "x"}}]},
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
            },
        ]
    )
    completed = [d for n, d in events if n == "response.completed"][0]
    assert completed["response"]["usage"] == {
        "input_tokens": 11,
        "output_tokens": 22,
        "total_tokens": 33,
    }


def test_finish_is_idempotent() -> None:
    # The proxy may reach its trailing emit through more than one path (normal
    # end, upstream error); closing twice must not duplicate items.
    translator = ResponsesStreamTranslator(model="m")
    translator.push({"choices": [{"delta": {"content": "hi"}}]})
    first = translator.finish()
    second = translator.finish()
    assert _events_of(first).count("response.output_item.done") == 1
    assert "response.output_item.done" not in _events_of(second)


def test_an_empty_stream_still_completes() -> None:
    events = _collect([])
    names = _events_of(events)
    assert names == ["response.created", "response.in_progress", "response.completed"]


def test_non_mapping_chunks_do_not_crash_the_translator() -> None:
    translator = ResponsesStreamTranslator(model="m")
    translator.start()
    assert translator.push({}) == []
    assert translator.push({"choices": [None]}) == []
    translator.finish()
