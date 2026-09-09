from __future__ import annotations

from types import SimpleNamespace

from app.services.muteki_writeup import render_muteki_writeup


def test_muteki_writeup_is_chinese_trace_driven_and_masks_sensitive_values() -> None:
    text = render_muteki_writeup(
        challenge=SimpleNamespace(name="企业合同审核平台", target_url="http://target.local:28346"),
        run=SimpleNamespace(id="run-wp", solver_mode="muteki", status="COMPLETED_SOLVED"),
        result=SimpleNamespace(status="COMPLETED_SOLVED", flag="flag{wp-proof}"),
        graph_state={
            "revision": 12,
            "facts": [
                {
                    "content": "classification=IDOR；/contracts/42 可访问",
                    "verified": True,
                    "evidence_refs": ["ev-42"],
                }
            ],
            "intents": [{"description": "验证合同对象访问边界", "status": "COMPLETED", "worker": "codex"}],
            "dead_ends": [{"description": "公共入口未发现 SQL 参数"}],
        },
        calls=[
            SimpleNamespace(
                id="call-42",
                tool_name="http_session_request",
                status="COMPLETED",
                arguments_json={
                    "method": "GET",
                    "url": "http://target.local:28346/contracts/42",
                    "headers": {"Cookie": "session=secret-cookie"},
                    "reason": "验证对象授权边界",
                },
            )
        ],
        observations=[
            SimpleNamespace(
                tool_call_id="call-42",
                observation_type="http_response",
                summary="HTTP 200，返回合同对象摘要",
                facts_json={"status_code": 200},
            )
        ],
        evidence=[
            SimpleNamespace(
                id="ev-42",
                evidence_type="HTTP_RESPONSE",
                status="VERIFIED",
                summary="对象 42 的响应已验证",
                tool_call_id="call-42",
                artifact_id=None,
            )
        ],
        poc_available=True,
    )

    assert "解题思路" in text
    assert "READ" in text and "OBSERVE" in text and "WRITE" in text
    assert "验证对象授权边界" in text
    assert "Evidence Ledger" in text
    assert "final/muteki-poc.zip" in text
    assert "flag{wp-proof}" in text
    assert "secret-cookie" not in text
    assert "{{secret_value}}" in text


def test_muteki_writeup_does_not_claim_poc_without_evidence() -> None:
    text = render_muteki_writeup(
        challenge=SimpleNamespace(name="未完成题", target_url="http://target.local"),
        run=SimpleNamespace(id="run-open", solver_mode="muteki", status="COMPLETED_UNSOLVED"),
        result=SimpleNamespace(status="COMPLETED_UNSOLVED", flag=None),
        graph_state={"revision": 1, "facts": [], "intents": [], "dead_ends": []},
        calls=[],
        observations=[],
        evidence=[],
        poc_available=False,
    )
    assert "不会用猜测内容伪造脚本" in text
    assert "Verified Flag" not in text
    assert "final/muteki-poc.zip" not in text


def test_muteki_writeup_reuses_completed_board_semantic_cache() -> None:
    text = render_muteki_writeup(
        challenge=SimpleNamespace(name="语义解析题", target_url="http://target.local"),
        run=SimpleNamespace(id="run-semantic", solver_mode="muteki", status="COMPLETED_SOLVED"),
        result=SimpleNamespace(status="COMPLETED_SOLVED", flag="flag{semantic-proof}"),
        graph_state={
            "revision": 8,
            "facts": [
                {
                    "sequence": 42,
                    "content": "POST /api/export returned a verified report URL.",
                    "verified": True,
                    "evidence_refs": ["ev-42"],
                }
            ],
            "intents": [],
            "dead_ends": [],
        },
        calls=[],
        observations=[],
        evidence=[],
        poc_available=False,
        semantic_analysis={
            "status": "completed",
            "model": "codex",
            "revision": 8,
            "items": [
                {
                    "card_id": "fact-42",
                    "summary_zh": "导出接口返回了可继续访问的报告地址。",
                    "category": "关键发现",
                    "importance": "high",
                }
            ],
        },
    )

    assert "AI 语义解析（与案情分析板同源）" in text
    assert "导出接口返回了可继续访问的报告地址" in text
    assert "AI 语义摘要" in text
    assert "POST /api/export returned a verified report URL" in text
