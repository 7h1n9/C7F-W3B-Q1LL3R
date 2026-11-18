"""Table-driven tests for the five-class fact grader.

The two traps that matter are pinned separately as well as in the table:

* environment setup text (``docker compose up -d`` / ``测试环境搭建``) must be
  ``unknown``, never ``executable``;
* a banner-only observation (``Apache/2.4.49``) must be ``observation``, never
  ``executable``.

The legacy (v1) rule is tested only as far as reconciliation needs it: it must
be the *old broad* rule, so it still calls a bare status+endpoint pair or a
named artifact executable, while the v2 rule does not.

The tests assert the rules as written.  There is no labelled corpus, so they
cannot and do not claim an accuracy rate.
"""

from __future__ import annotations

import pytest

from app.services.fact_quality import (
    EXECUTABLE_DEFINITION_VERSION,
    FACT_CLASSES,
    LEGACY_EXECUTABLE_DEFINITION_VERSION,
    classify_fact,
    classify_fact_legacy,
    executable_fact_summary,
    is_executable_fact,
    is_executable_fact_legacy,
)


# ---------------------------------------------------------------------------
# unknown
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
def test_placeholders_setup_and_empty_are_unknown(text: str) -> None:
    assert classify_fact(text) == "unknown"
    assert is_executable_fact(text) is False


def test_setup_text_with_a_real_command_word_is_still_unknown() -> None:
    """The trap: a command-looking setup line is not a fact about the target."""

    text = "docker compose up -d && curl -s http://localhost:8000/health"
    assert classify_fact(text) == "unknown"
    assert is_executable_fact(text) is False


# ---------------------------------------------------------------------------
# observation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Apache/2.4.49",
        "Server: nginx/1.18.0",
        "Target is phpMyAdmin 4.8.1 on Apache/2.4.25 (Debian), PHP/7.2.5",
        "GET /admin returns 403",
        "robots.txt disallows /static and /admin",
        "The users table has columns id, username, password",
        "www.zip contains the full application source",
        "Host 117.21.200.176 is up and serves a default page",
        "Deployed: /uddiexplorer/ and /_async/AsyncResponseService both return 200",
        "HugeGraph 1.3.0 is running; / returns a JSON version banner",
    ],
)
def test_banners_status_and_endpoint_listings_are_observation(text: str) -> None:
    assert classify_fact(text) == "observation"
    assert is_executable_fact(text) is False


def test_version_banner_is_observation_not_executable() -> None:
    """Required trap (b): a banner is witnessed, but nothing can be replayed."""

    assert classify_fact("Apache/2.4.49") == "observation"
    assert is_executable_fact("Apache/2.4.49") is False


# ---------------------------------------------------------------------------
# hypothesis
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "The app uses JWT for authentication",
        "Maybe SQL injection in the id parameter",
        "The exploit likely works because the version is old",
        "Next step is to try SSRF against the metadata endpoint",
        "CVE-2012-1823 exploitation confirmed with RCE as www-data user",
        "The app appears to trust the X-Forwarded-For header",
        "We should enumerate vhosts next",
        "可能是 ThinkPHP 框架",
    ],
)
def test_claims_and_plans_without_a_witness_are_hypothesis(text: str) -> None:
    assert classify_fact(text) == "hypothesis"
    assert is_executable_fact(text) is False


# ---------------------------------------------------------------------------
# negative
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "[claude] DEADEND=s2-016 `redirect:${...}` POST to example5.action -> 400",
        "LFI confirmed on the live target. No flag was found in this lane.",
        "Port 8080 refused connections",
        "The upload form is not vulnerable",
        "上传点不可用",
        "404 for every id, including 3 and 11",
        "The target never accepted a connection",
        "Connection timed out after 10s",
    ],
)
def test_dead_ends_and_negative_results_are_negative(text: str) -> None:
    assert classify_fact(text) == "negative"
    assert is_executable_fact(text) is False


def test_negative_wins_over_a_quoted_payload() -> None:
    """A dead end that quotes ``id=1`` is a warning, not a command to run."""

    text = "DEADEND: ?id=1' OR '1'='1 returns 500 on every endpoint"
    assert classify_fact(text) == "negative"
    assert is_executable_fact(text) is False


# ---------------------------------------------------------------------------
# executable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "`curl -s http://10.0.0.5:8080/admin` returns 200",
        "ssh -p 26877 -i vulhub id_rsa git@target \"git-upload-archive '--help'\"",
        "GET /index.php?target=../../../../etc/passwd returns 200",
        'POST /gremlin with {"gremlin":"System.getenv(\'FLAG\')"}',
        "UNION SELECT username,password FROM users",
        "SSTI confirmed: {{7*7}} renders 49",
        "LFI reads /etc/passwd",
        "/var/www/html/flag.php contains the flag",
        "Cookie: PHPSESSID=deadbeefcafe; role=admin",
        "console login weblogic:Oracle@123 succeeds",
        "flag{7h1s_1s_4_fl4g}",
        "`id` returns uid=0(root) gid=0(root)",
        "`printenv FLAG` returns the flag",
        "POST /login with username=admin&password=admin",
        "GET /api/user?id=1' OR '1'='1",
    ],
)
def test_actionable_artifacts_are_executable(text: str) -> None:
    assert classify_fact(text) == "executable"
    assert is_executable_fact(text) is True


def test_paraphrase_is_not_executable() -> None:
    """A technology name alone is a claim; it does not become a command."""

    assert is_executable_fact("The application uses the JWT standard") is False
    assert is_executable_fact("Authorization is handled by a JWT") is False


# ---------------------------------------------------------------------------
# v1 legacy rule used for reconciliation
# ---------------------------------------------------------------------------


def test_definition_versions_are_pinned() -> None:
    assert EXECUTABLE_DEFINITION_VERSION == "v2"
    assert LEGACY_EXECUTABLE_DEFINITION_VERSION == "v1"


@pytest.mark.parametrize(
    "text",
    [
        "GET /admin returns 403",
        "robots.txt disallows /static and /admin",
        "Host 117.21.200.176 is up and serves a default page",
        "Backup at /tmp/db.sql contains the users table",
    ],
)
def test_legacy_rule_still_counts_concrete_artifacts(text: str) -> None:
    """These are the facts v2 reclassifies; v1 must keep calling them executable."""

    assert is_executable_fact_legacy(text) is True
    assert classify_fact(text) != "executable"


def test_legacy_rule_keeps_the_noise_and_banner_exemptions() -> None:
    assert classify_fact_legacy("docker compose up -d") == "noise"
    assert classify_fact_legacy("") == "noise"
    # A bare banner was already excluded under v1; only the strictness of v2
    # about *concrete but non-actionable* artifacts moved the ratio.
    assert is_executable_fact_legacy("Apache/2.4.49") is False


def test_legacy_rule_still_sees_a_command() -> None:
    assert classify_fact_legacy("GET /api/user?id=1' OR '1'='1") == "executable"
    assert classify_fact_legacy("The app uses JWT") == "narrative"


# ---------------------------------------------------------------------------
# summary and invariants
# ---------------------------------------------------------------------------


def test_summary_contains_all_five_classes_and_ratio() -> None:
    facts = [
        "docker compose up -d",  # unknown
        "Apache/2.4.49",  # observation
        "The app uses JWT",  # hypothesis
        "Port 8080 refused connections",  # negative
        "GET /x?id=1' OR '1'='1",  # executable
    ]
    summary = executable_fact_summary(facts)

    assert summary["total"] == 5
    assert summary["executable"] == 1
    assert summary["ratio"] == 0.2
    assert summary["by_class"] == {
        "executable": 1,
        "observation": 1,
        "hypothesis": 1,
        "negative": 1,
        "unknown": 1,
    }
    assert set(summary["by_class"]) == set(FACT_CLASSES)


def test_summary_carries_both_definition_versions() -> None:
    """The new number can never be printed as if it were the old one."""

    facts = [
        "Apache/2.4.49",  # v2 observation, v1 not executable (banner-only)
        "GET /admin returns 403",  # v2 observation, v1 executable
        "GET /x?id=1' OR '1'='1",  # both executable
        "docker compose up -d",  # both unknown/noise
    ]
    summary = executable_fact_summary(facts)

    assert summary["definition_version"] == EXECUTABLE_DEFINITION_VERSION
    assert summary["legacy_definition_version"] == LEGACY_EXECUTABLE_DEFINITION_VERSION
    assert summary["executable"] == 1
    assert summary["executable_legacy"] == 2
    assert summary["moved_from_legacy"] == 1
    assert summary["legacy_ratio"] == 0.5


def test_summary_empty_and_single_string() -> None:
    empty = executable_fact_summary([])
    assert empty["total"] == 0
    assert empty["executable"] == 0
    assert empty["ratio"] == 0.0
    assert empty["by_class"] == {name: 0 for name in FACT_CLASSES}
    assert empty["executable_legacy"] == 0
    assert empty["legacy_ratio"] == 0.0
    assert empty["moved_from_legacy"] == 0

    one = executable_fact_summary("GET /x?id=1")
    assert one["total"] == 1
    assert one["executable"] == 1


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
    assert classify_fact(text) == "hypothesis"
    assert classify_fact(text + " Apache/2.4.49") == "observation"
    assert classify_fact(text + " /etc/passwd") == "executable"
    assert classify_fact(text + " /etc/passwd") == classify_fact(text + " /etc/passwd")


def test_long_unbroken_text_does_not_hang() -> None:
    assert classify_fact("A" * 50_000) == "hypothesis"
    assert classify_fact("A" * 50_000 + " /etc/passwd") == "executable"
