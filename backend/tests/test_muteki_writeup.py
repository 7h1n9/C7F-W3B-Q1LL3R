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
    assert "读取" in text and "观察" in text and "写入" in text
    assert "验证对象授权边界" in text
    assert "证据台账" in text
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
    assert "已验证 Flag" not in text
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


def test_writeup_user_facing_text_is_chinese() -> None:
    """The report is for a Chinese reader: no bare English section labels.

    Regression cover for "优化一下 poc 和 wp 里的解释说明文字…尽可能中文":
    headings, field names, step markers and evidence enums were all English even
    though every surrounding sentence was Chinese.
    """

    text = render_muteki_writeup(
        challenge=SimpleNamespace(name="中文题", target_url="http://target.local"),
        run=SimpleNamespace(id="run-zh", solver_mode="muteki", status="COMPLETED_SOLVED"),
        result=SimpleNamespace(status="COMPLETED_SOLVED", flag="CTF2{zh-proof}"),
        graph_state={
            "revision": 2,
            "facts": [{"sequence": 1, "verified": True, "content": "确认了越权访问", "evidence_refs": ["ev-1"]}],
            "dead_ends": [{"description": "先试了默认口令，失败"}],
            "intents": [],
        },
        calls=[
            SimpleNamespace(
                tool_name="http_request",
                normalized_arguments={"method": "GET", "url": "http://target.local/api/item?id=1"},
                status="COMPLETED",
            )
        ],
        observations=[],
        evidence=[
            SimpleNamespace(
                id="ev-1",
                evidence_type="HTTP_RESPONSE",
                status="VERIFIED",
                summary="响应已验证",
                tool_call_id="c1",
                artifact_id=None,
            )
        ],
        poc_available=True,
    )

    # Headings and field labels.
    assert "已解出" in text
    assert "未运行" in text
    assert "证据台账" in text
    assert "完成门禁" in text
    assert "死胡同" in text
    # Step markers are localized (the ASCII enums stay internal).
    assert "读取" in text or "观察" in text or "写入" in text
    # Evidence enums are glossed rather than dumped raw.
    assert "HTTP 响应" in text and "已验证" in text
    assert "HTTP_RESPONSE · VERIFIED" not in text
    # No bare English headings survived.
    for token in ("## CTF Writeup\n", "Evidence Ledger\n", "Dead End:", "Verified Flag："):
        assert token not in text


def test_vulnerability_class_keeps_its_ascii_code_visible() -> None:
    """A Chinese gloss must not hide the machine-readable class."""

    from app.services.muteki_writeup import _vuln_type_label

    assert _vuln_type_label("SQLI") == "SQL 注入（SQLI）"
    assert _vuln_type_label("MODEL_INVERSION") == "模型反演（MODEL_INVERSION）"
    # An unknown class passes through rather than being dropped.
    assert _vuln_type_label("SOMETHING_NEW") == "SOMETHING_NEW"
    assert _vuln_type_label("") == ""


def test_composite_vulnerability_label_glosses_every_part() -> None:
    """The "model vs heuristic" composite must not leave a code untranslated."""

    from app.services.muteki_writeup import _vuln_type_label

    text = _vuln_type_label("ARBITRARY_FILE_WRITE（模型判定）/ GENERIC_WEB（启发式）")
    assert "任意文件写入" in text
    assert "通用 Web 漏洞" in text
    # Both ASCII codes stay visible for greppability.
    assert "ARBITRARY_FILE_WRITE" in text and "GENERIC_WEB" in text
    # An unknown code passes through with its annotation intact.
    assert _vuln_type_label("UNKNOWN_X（模型判定）") == "UNKNOWN_X（模型判定）"
