"""Table-driven tests for the next-direction service.

The two properties that matter most here are asserted explicitly and in
isolation: an infrastructure abort must never produce a research direction or
enter the solve-rate denominator, and a solver failure must never be laundered
into an infrastructure abort by a word inside an exploit payload.
"""

from __future__ import annotations

import pytest

from app.services.next_direction import (
    INFRA_ERROR_CODES,
    SOLVED_STATUSES,
    VULN_CLASSES,
    Outcome,
    classify_outcome,
    classify_text,
    compose_next_direction,
    untried_vuln_classes,
)

SOLVER_OUTCOME = Outcome("unsolved_solver", (), False)
INFRA_OUTCOME = Outcome("aborted_infra", ("模型账户余额不足",), True)


# --------------------------------------------------------------------------- #
# classify_outcome: solved vs unsolved vs aborted
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("kwargs", "expected_class", "excluded"),
    [
        ({"status": "COMPLETED_SOLVED"}, "solved", False),
        ({"status": "SOLVED"}, "solved", False),
        ({"status": "COMPLETED_UNSOLVED", "has_flag": True}, "solved", False),
        # The trap: prefix membership once made this look solved.
        ({"status": "COMPLETED_UNSOLVED"}, "unsolved_solver", False),
        ({"status": "FAILED_TOOL"}, "unsolved_solver", False),
        ({"status": "TIMEOUT"}, "unsolved_solver", False),
        ({"status": "CANCELLED", "command_count": 7}, "unsolved_solver", False),
        ({"status": "UNKNOWN_STATUS"}, "unsolved_solver", False),
    ],
)
def test_outcome_classification_table(kwargs, expected_class, excluded):
    outcome = classify_outcome(**kwargs)
    assert outcome.outcome_class == expected_class
    assert outcome.excluded_from_solve_rate is excluded
    if expected_class != "aborted_infra":
        assert outcome.infra_reasons == ()


@pytest.mark.parametrize(
    "code",
    [
        "INSUFFICIENT_BALANCE",
        "INSUFFICIENT_QUOTA",
        "MODEL_QUOTA_EXCEEDED",
        "HTTP_402",
        "HTTP_429",
        "PROVIDER_DENIED",
        "OPENAI_COMPATIBLE_PROVIDER_DENIED",
        "TARGET_UNAVAILABLE",
        "BACKEND_UNAVAILABLE",
        "RUNNER_UNAVAILABLE",
        "TARGET_UNREACHABLE",
    ],
)
def test_infra_error_codes_abort_and_exclude(code):
    outcome = classify_outcome(status="FAILED_ENGINE", error_code=code, command_count=12)
    assert outcome.outcome_class == "aborted_infra"
    assert outcome.excluded_from_solve_rate is True
    assert outcome.infra_reasons, "an infra abort must explain itself"


def test_infra_error_code_is_uppercased_before_membership():
    outcome = classify_outcome(status="FAILED_ENGINE", error_code=" insufficient_balance ")
    assert outcome.outcome_class == "aborted_infra"
    assert outcome.excluded_from_solve_rate is True


@pytest.mark.parametrize(
    "message",
    [
        "upstream returned HTTP 402 Payment Required",
        "provider says 429 Too Many Requests, retry later",
        "HTTP/1.1 502 Bad Gateway from ingress",
        "503 Service Unavailable",
        "proxy connect error: ECONNREFUSED 127.0.0.1:7890",
        "ssl handshake failure while talking to the proxy",
        "timed out connecting to proxy at 127.0.0.1:38261",
        "connection refused by upstream provider",
        "insufficient_quota: you exceeded your current quota",
        "模型账户余额不足，无法继续生成",
        "目标不可达，runner 无法建立隧道",
    ],
)
def test_infra_message_phrases_abort(message):
    outcome = classify_outcome(status="FAILED_ENGINE", error_message=message, command_count=9)
    assert outcome.outcome_class == "aborted_infra"
    assert outcome.excluded_from_solve_rate is True


@pytest.mark.parametrize(
    "message",
    [
        # A legitimate exploit command that merely contains the word timeout.
        "timeout 5 curl -s http://10.10.10.5/flag",
        "python -c \"import socket; socket.setdefaulttimeout(3); print(1)\"",
        "payload=1' UNION SELECT 'timeout'-- -",
        # Anchored phrases only: bare 500s and bare 402/429 are not evidence.
        "target returned 500 on /login",
        "the port column says 502",
    ],
)
def test_solver_payload_text_does_not_become_infra(message):
    outcome = classify_outcome(
        status="COMPLETED_UNSOLVED",
        error_message=message,
        command_count=42,
    )
    assert outcome.outcome_class == "unsolved_solver"
    assert outcome.excluded_from_solve_rate is False
    assert outcome.infra_reasons == ()


def test_long_tool_transcript_is_not_scanned_for_infra_phrases():
    transcript = (
        "tool output follows: " + ("A" * 500)
        + " 503 Service Unavailable appeared inside the leaked response body"
    )
    outcome = classify_outcome(status="COMPLETED_UNSOLVED", error_message=transcript)
    assert outcome.outcome_class == "unsolved_solver"


def test_manual_shutdown_before_any_command_is_infra():
    outcome = classify_outcome(status="CANCELLED", command_count=0)
    assert outcome.outcome_class == "aborted_infra"
    assert outcome.excluded_from_solve_rate is True
    assert any("手动终止" in reason for reason in outcome.infra_reasons)


def test_solved_status_constant_never_contains_unsolved():
    assert "COMPLETED_UNSOLVED" not in SOLVED_STATUSES
    assert "COMPLETED_SOLVED" in SOLVED_STATUSES
    assert isinstance(INFRA_ERROR_CODES, frozenset)
    assert "insufficient_balance" not in INFRA_ERROR_CODES  # exact/upper only


# --------------------------------------------------------------------------- #
# classify_text / untried_vuln_classes
# --------------------------------------------------------------------------- #


def test_classify_text_mixed_chinese_and_english():
    text = "先测 SQL 注入，再打 XSS 反射型，最后用 SSRF 做内网探测"
    assert classify_text(text) == {"sql_injection", "xss", "ssrf"}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("尝试模板注入 SSTI 读取配置", {"ssti"}),
        ("fastjson 反序列化链", {"deserialization"}),
        ("LFI via php://filter", {"file_include"}),
        ("目录遍历 ../ 拿到备份文件", {"path_traversal", "information_disclosure"}),
        ("命令注入拿到 RCE", {"command_injection"}),
        ("弱口令进后台，再水平越权", {"auth_bypass", "idor"}),
        ("XXE 外部实体读文件", {"xxe"}),
        ("竞态条件竞争优惠券", {"race_condition"}),
        ("JWT none 算法绕过", {"weak_crypto"}),
    ],
)
def test_classify_text_keyword_table(text, expected):
    assert classify_text(text) == expected


def test_classify_text_unknown_is_empty_frozenset():
    result = classify_text("nothing security-specific here")
    assert result == frozenset()
    assert isinstance(result, frozenset)


def test_classify_text_respects_ascii_word_boundaries():
    assert "xss" not in classify_text("the string fizzssbox and boxxer")
    assert classify_text("") == frozenset()


def test_untried_vuln_classes_is_sorted_and_excludes_attempted():
    attempted = ["SQL 注入 报错注入", "反射型 XSS", "unknown topic"]
    result = untried_vuln_classes(attempted)
    assert isinstance(result, tuple)
    assert result == tuple(sorted(result))
    assert "sql_injection" not in result
    assert "xss" not in result
    assert "ssti" in result
    assert set(result) <= set(VULN_CLASSES)


def test_untried_vuln_classes_honours_explicit_candidates_and_is_repeatable():
    first = untried_vuln_classes(["SSRF 内网"], ["ssrf", "xxe", "ssti"])
    second = untried_vuln_classes(["SSRF 内网"], ["ssrf", "xxe", "ssti"])
    assert first == ("ssti", "xxe")
    assert first == second


# --------------------------------------------------------------------------- #
# compose_next_direction: the seven precedence branches
# --------------------------------------------------------------------------- #


def test_branch_1_solved_returns_none():
    solved = Outcome("solved", (), False)
    assert compose_next_direction(
        outcome=solved,
        open_flag_candidates=("flag{maybe}",),
        verified_facts=("admin panel at /admin",),
    ) is None


def test_branch_1_infra_returns_none_even_with_rich_inputs():
    direction = compose_next_direction(
        outcome=INFRA_OUTCOME,
        open_flag_candidates=("flag{looks_real}",),
        verified_facts=("db is mysql 8",),
        untried_classes=("ssrf", "xxe"),
        dead_ends=("union select filtered",),
        total_facts=5,
        command_count=30,
    )
    assert direction is None


def test_branch_2_open_flag_candidate_wins_over_everything_later():
    direction = compose_next_direction(
        outcome=SOLVER_OUTCOME,
        open_flag_candidates=("flag{abc}", "CTF{def}"),
        verified_facts=("admin password rotated",),
        untried_classes=("ssrf",),
        dead_ends=("ssti blocked",),
        total_facts=3,
        command_count=2,
    )
    assert direction is not None
    assert direction.source == "open_flag_candidate"
    assert direction.confidence == "high"
    assert "复核候选 flag" in direction.action
    assert direction.evidence == ("flag{abc}", "CTF{def}")


def test_branch_3_verified_foothold_unused():
    direction = compose_next_direction(
        outcome=SOLVER_OUTCOME,
        verified_facts=("SQLi boolean oracle confirmed on id",),
        untried_classes=("ssrf", "xxe"),
        dead_ends=("waf blocks union",),
        total_facts=1,
        command_count=2,
    )
    assert direction is not None
    assert direction.source == "verified_foothold_unused"
    assert direction.confidence == "high"
    assert "已验证事实" in direction.action
    assert direction.evidence == ("SQLi boolean oracle confirmed on id",)


def test_branch_3_does_not_fire_when_commands_were_used():
    direction = compose_next_direction(
        outcome=SOLVER_OUTCOME,
        verified_facts=("SQLi boolean oracle confirmed on id",),
        total_facts=1,
        command_count=9,
    )
    assert direction is not None
    assert direction.source == "insufficient_evidence"


def test_branch_4_dead_end_review_uses_last_three_oldest_first():
    dead_ends = ("first", "second", "third", "fourth", "fifth")
    direction = compose_next_direction(
        outcome=SOLVER_OUTCOME,
        dead_ends=dead_ends,
        total_facts=4,
        command_count=30,
    )
    assert direction is not None
    assert direction.source == "dead_end_review"
    assert direction.confidence == "medium"
    assert direction.evidence == ("third", "fourth", "fifth")
    assert "fifth" in direction.action
    assert "判据" in direction.action
    assert "假设" in direction.rationale


def test_branch_4_dead_end_beats_untried_classes_when_both_present():
    direction = compose_next_direction(
        outcome=SOLVER_OUTCOME,
        dead_ends=("SSTI 探针原样返回",),
        untried_classes=("ssrf", "xxe"),
        total_facts=4,
        command_count=30,
    )
    assert direction is not None
    assert direction.source == "dead_end_review"
    assert direction.confidence == "medium"
    assert direction.evidence == ("SSTI 探针原样返回",)


def test_branch_5_untried_class_is_a_check_not_a_recommendation():
    direction = compose_next_direction(outcome=SOLVER_OUTCOME, untried_classes=("ssrf", "xxe"))
    assert direction is not None
    assert direction.source == "untried_class"
    assert direction.confidence == "medium"
    assert "前置条件" in direction.action
    assert "ssrf" in direction.action and "xxe" in direction.action
    assert "覆盖缺口" in direction.rationale
    assert direction.evidence == ("ssrf", "xxe")


def test_branch_5_untried_class_single_is_low_confidence():
    direction = compose_next_direction(outcome=SOLVER_OUTCOME, untried_classes=("ssrf",))
    assert direction is not None
    assert direction.source == "untried_class"
    assert direction.confidence == "low"


def test_branch_6_no_fact_run_is_low_confidence():
    direction = compose_next_direction(outcome=SOLVER_OUTCOME, total_facts=0, command_count=11)
    assert direction is not None
    assert direction.source == "no_fact"
    assert direction.confidence == "low"
    assert "探针" in direction.action
    assert direction.evidence


def test_branch_7_insufficient_evidence_states_what_is_missing():
    direction = compose_next_direction(outcome=SOLVER_OUTCOME, total_facts=3, command_count=15)
    assert direction is not None
    assert direction.source == "insufficient_evidence"
    assert direction.confidence == "low"
    assert "观测" in direction.action
    assert direction.evidence == ("total_facts=3", "command_count=15")


def test_precedence_open_flag_beats_untried_and_dead_end():
    only_flag = compose_next_direction(
        outcome=SOLVER_OUTCOME, open_flag_candidates=("FLAG{x}",), untried_classes=("ssrf",)
    )
    only_untried = compose_next_direction(outcome=SOLVER_OUTCOME, untried_classes=("ssrf",))
    assert only_flag is not None and only_untried is not None
    assert only_flag.source == "open_flag_candidate"
    assert only_untried.source == "untried_class"


def test_precedence_foothold_beats_dead_end():
    direction = compose_next_direction(
        outcome=SOLVER_OUTCOME,
        verified_facts=("LFI confirmed",),
        dead_ends=("upload rejected",),
        command_count=1,
    )
    assert direction is not None
    assert direction.source == "verified_foothold_unused"


def test_blank_inputs_do_not_fire_a_branch():
    direction = compose_next_direction(
        outcome=SOLVER_OUTCOME,
        open_flag_candidates=("", "   "),
        verified_facts=(),
        untried_classes=(),
        dead_ends=(),
        total_facts=2,
        command_count=8,
    )
    assert direction is not None
    assert direction.source == "insufficient_evidence"


# --------------------------------------------------------------------------- #
# determinism and evidence bounds
# --------------------------------------------------------------------------- #


def test_compose_is_deterministic():
    kwargs = dict(
        outcome=SOLVER_OUTCOME,
        dead_ends=("dead A", "dead B"),
        verified_facts=("fact A",),
        untried_classes=("ssti", "xxe"),
        open_flag_candidates=(),
        total_facts=2,
        command_count=40,
    )
    assert compose_next_direction(**kwargs) == compose_next_direction(**kwargs)


def test_evidence_is_bounded_to_five_items_of_300_chars():
    long_items = tuple(f"candidate-{index}-" + "x" * 500 for index in range(10))
    direction = compose_next_direction(
        outcome=SOLVER_OUTCOME,
        open_flag_candidates=long_items,
    )
    assert direction is not None
    assert len(direction.evidence) == 5
    assert all(len(item) <= 300 for item in direction.evidence)
    assert all(item.startswith("candidate-") for item in direction.evidence)


def test_dead_end_evidence_is_bounded_too():
    long_dead_ends = tuple("y" * 1000 for _ in range(3))
    direction = compose_next_direction(outcome=SOLVER_OUTCOME, dead_ends=long_dead_ends)
    assert direction is not None
    assert direction.source == "dead_end_review"
    assert len(direction.evidence) <= 5
    assert all(len(item) <= 300 for item in direction.evidence)


# --------------------------------------------------------------------------- #
# coverage-gap degeneracy: a blind detector must not be reported as a gap
# --------------------------------------------------------------------------- #


def test_full_coverage_gap_is_reported_as_an_unknown_not_a_checklist():
    """Every class untried means the detector saw nothing, not that the Run
    tried nothing. Measured on 15,111 real shell invocations the keyword
    detector matches a class in 4.1% of command lines, so this branch is the
    common case and it must not emit fifteen items of advice."""
    direction = compose_next_direction(outcome=SOLVER_OUTCOME, untried_classes=VULN_CLASSES)

    assert direction is not None
    assert direction.source == "coverage_unknown"
    assert direction.confidence == "low"
    # And it must not smuggle the full list back in through the action text.
    assert not any(vuln in direction.action for vuln in VULN_CLASSES)


def test_untried_class_action_is_bounded_to_four_items():
    direction = compose_next_direction(
        outcome=SOLVER_OUTCOME,
        untried_classes=("ssrf", "xxe", "ssti", "deserialization", "xss", "idor"),
    )

    assert direction is not None
    assert direction.source == "untried_class"
    assert len(direction.evidence) == 4
    assert "xss" not in direction.action and "idor" not in direction.action
