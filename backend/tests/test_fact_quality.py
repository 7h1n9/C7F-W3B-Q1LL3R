"""Tests for the three-class fact grader.

The taxonomy is the project's established one -- ``executable`` / ``narrative``
/ ``noise`` -- so the measured ratio stays comparable to earlier runs.  The
five-way split is tested only as an additional diagnostic field.

Two traps are pinned explicitly and also appear in the table:

* setup text (``docker compose up -d`` / ``测试环境搭建``) is noise, never
  executable;
* a banner-only string (``Apache/2.4.49``) is narrative, never executable.
"""

from __future__ import annotations

import pytest

from app.services.fact_quality import (
    DETAIL_CLASSES,
    FACT_CLASSES,
    classify_fact,
    classify_fact_detail,
    executable_fact_summary,
    is_executable_fact,
)


# ---------------------------------------------------------------------------
# noise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   \n\t  ",
        "Native Worker completed a bounded evidence-backed observation",
        "[claude] (no output)",
        "[codex] NEED_KIND=external_blocker",
        "[codex] NEED_INPUT=none — recon lane complete; no external blocker.",
        "[codex] `NEED_INPUT=none` — recon lane complete; no external blocker.",
        "[claude] FOUND_FLAG=NONE",
        '{"observation_status":"UNAVAILABLE","tool":"http_request"}',
        '[codex] {"type":"turn.started"}',
        "测试环境搭建",
        "docker compose up -d",
        "pip install -r requirements.txt",
        "Flask app 已启动",
        "The server is running on port 8000",
    ],
)
def test_placeholders_setup_and_empty_are_noise(text: str) -> None:
    assert classify_fact(text) == "noise"
    assert is_executable_fact(text) is False


def test_setup_text_with_a_real_command_word_is_still_noise() -> None:
    """Required trap (a): setup is not a fact about the target."""

    text = "docker compose up -d && curl -s http://localhost:8000/health"
    assert classify_fact(text) == "noise"
    assert is_executable_fact(text) is False


# ---------------------------------------------------------------------------
# narrative
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Apache/2.4.49",
        "Server: nginx/1.18.0",
        "Server: Werkzeug/2.0.1 Python/3.8.10",
        "Python 3.12.13",
        "The app uses JWT for authentication",
        "Maybe SQL injection in the id parameter",
        "LFI confirmed on the live target. Now reading sensitive files through it.",
        "CVE-2012-1823 exploitation confirmed with RCE as www-data user",
        "No flag was found in this lane",
        "Port 8080 refused connections",
        "上传点不可用",
        "404 for every id, including 3 and 11",
    ],
)
def test_claims_banners_and_dead_ends_are_narrative(text: str) -> None:
    assert classify_fact(text) == "narrative"
    assert is_executable_fact(text) is False


def test_banner_only_is_narrative_not_executable() -> None:
    """Required trap (b): a banner is witnessed, but nothing can be replayed."""

    assert classify_fact("Apache/2.4.49") == "narrative"
    assert is_executable_fact("Apache/2.4.49") is False


def test_banner_inside_a_sentence_is_still_executable() -> None:
    """Only a *banner-only* string is downgraded; a real sentence keeps its win."""

    text = "Target runs Apache/2.4.49 (Unix) on port 80"
    assert classify_fact(text) == "executable"
    assert is_executable_fact(text) is True


# ---------------------------------------------------------------------------
# executable: every artifact family
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Target at http://10.0.0.5:8080/login",
        "direct-ctf2.dasctf.com:28072 runs Tomcat AJP",
        "Host 117.21.200.176 answers on port 80",
        "LFI reads /etc/passwd and /var/www/html/index.php",
        "recovered shared/recon/reflect1.txt from the workspace",
        "index.php?-s dumps the application source",
        "Python 3.12.13 powers the app",
        '{"endpoint": "/admin", "status_code": 403}',
        "id=1 returns the admin row",
        "ids[0,updatexml(1,concat(0x7e),1)]=1 triggers the error page",
        "console login weblogic:Oracle@123 succeeds",
        "Cookie: PHPSESSID=deadbeefcafe",
        "?file=../../../../etc/passwd is readable",
        "SSTI confirmed: {{7*7}} renders 49",
        "UNION SELECT username,password FROM users",
        "flag{7h1s_1s_4_fl4g} appeared in the response",
        "CTF2{87486f1c-5bf2-4532-a7f6-de16fd0555c1}",
    ],
)
def test_actionable_artifacts_are_executable(text: str) -> None:
    assert classify_fact(text) == "executable"
    assert is_executable_fact(text) is True


# ---------------------------------------------------------------------------
# detail field (additional, never a replacement)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "detail"),
    [
        ("docker compose up -d", "unknown"),
        ("Apache/2.4.49", "observation"),
        ("The app uses JWT", "hypothesis"),
        ("No flag was found", "negative"),
        ("GET /x?id=1", "executable"),
    ],
)
def test_detail_split_is_additive(text: str, detail: str) -> None:
    assert classify_fact_detail(text) == detail
    assert detail in DETAIL_CLASSES


def test_detail_mapping_never_contradicts_three_class() -> None:
    corpus = [
        "Apache/2.4.49",
        "docker compose up -d",
        "maybe SQLi",
        "no flag found",
        "`curl -s http://x/`",
    ]
    for text in corpus:
        label = classify_fact(text)
        detail = classify_fact_detail(text)
        if label == "executable":
            assert detail == "executable"
        elif label == "noise":
            assert detail == "unknown"
        else:
            assert detail in {"observation", "hypothesis", "negative"}


# ---------------------------------------------------------------------------
# summary and invariants
# ---------------------------------------------------------------------------


def test_summary_keeps_three_class_fields_and_adds_detail() -> None:
    facts = [
        "GET /x?id=1",  # executable
        "LFI reads /etc/passwd",  # executable
        "Apache/2.4.49",  # narrative / observation
        "No flag was found",  # narrative / negative
        "docker compose up -d",  # noise / unknown
    ]
    summary = executable_fact_summary(facts)

    assert summary["total"] == 5
    assert summary["counts"] == {"executable": 2, "narrative": 2, "noise": 1}
    assert summary["shares"] == {
        "executable": 0.4,
        "narrative": 0.4,
        "noise": 0.2,
    }
    assert summary["executable_per_100"] == 40.0
    assert summary["non_noise"] == 4
    assert summary["executable_share_of_non_noise"] == 0.5
    assert summary["by_detail"] == {
        "executable": 2,
        "observation": 1,
        "hypothesis": 0,
        "negative": 1,
        "unknown": 1,
    }
    assert set(summary["counts"]) == set(FACT_CLASSES)
    assert set(summary["by_detail"]) == set(DETAIL_CLASSES)


def test_summary_empty_and_single_string() -> None:
    empty = executable_fact_summary([])
    assert empty["total"] == 0
    assert empty["counts"] == {name: 0 for name in FACT_CLASSES}
    assert empty["executable_per_100"] == 0.0
    assert empty["executable_share_of_non_noise"] == 0.0

    one = executable_fact_summary("GET /x?id=1")
    assert one["total"] == 1
    assert one["counts"]["executable"] == 1


def test_every_label_is_in_the_declared_set() -> None:
    corpus = [
        "",
        "Apache/2.4.49",
        "maybe SQLi",
        "DEADEND=x",
        "`curl -s http://x/`",
        "docker compose up -d",
    ]
    for text in corpus:
        assert classify_fact(text) in FACT_CLASSES


def test_long_text_is_bounded_and_deterministic() -> None:
    text = "the worker kept trying " * 2000  # ~48k chars
    assert classify_fact(text) == "narrative"
    assert classify_fact(text + " /etc/passwd") == "executable"
    assert classify_fact(text + " /etc/passwd") == classify_fact(text + " /etc/passwd")


def test_long_unbroken_text_does_not_hang() -> None:
    assert classify_fact("A" * 50_000) == "narrative"
    assert classify_fact("A" * 50_000 + " /etc/passwd") == "executable"
