from app.services.muteki_board_semantics import (
    _redact,
    board_fingerprint,
    build_board_inputs,
    parse_bridge_events,
)


def test_board_inputs_are_bounded_and_redacted() -> None:
    snapshot = {
        "key_conditions": [
            {
                "sequence": 42,
                "summary_zh": "账号 password=secret-pass 可登录",
                "evidence_refs": ["evidence-1"],
            }
        ],
    }

    rows = build_board_inputs(snapshot)

    assert rows[0] == {
        "card_id": "fact-42",
        "sequence": 42,
        "local_summary": "账号 password=[REDACTED] 可登录",
        "evidence_count": 1,
    }
    assert len(rows) == 1
    assert "do-not-send" not in _redact("flag{do-not-send}")


def test_board_fingerprint_changes_with_revision_or_facts() -> None:
    items = [{"card_id": "fact-1", "local_summary": "接口已确认"}]
    assert board_fingerprint(1, items) != board_fingerprint(2, items)
    assert board_fingerprint(1, items) != board_fingerprint(1, [{"card_id": "fact-1", "local_summary": "接口已变化"}])


def test_parse_bridge_events_accepts_only_expected_structured_cards() -> None:
    events = [
        {
            "type": "agent.message",
            "payload": {
                "message": '{"items":[{"card_id":"fact-1","summary_zh":"接口需要认证","category":"认证边界","importance":"high"},{"card_id":"fact-unknown","summary_zh":"忽略","category":"其他","importance":"low"}]}'
            },
        },
        {"type": "agent.turn_completed", "payload": {"usage": {"input_tokens": 10, "output_tokens": 5}}},
    ]

    items, usage = parse_bridge_events(events, {"fact-1"})

    assert items == [{"card_id": "fact-1", "summary_zh": "接口需要认证", "category": "认证边界", "importance": "high"}]
    assert usage == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
