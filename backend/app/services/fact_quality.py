"""Grade a board fact by whether it can become a verification command.

The project's headline is "facts per 100 commands".  That number is inflated by
text which carries no knowledge: template placeholders, environment setup, and
progress narration.  This module keeps the project's established three-class
taxonomy so historical measurements stay comparable:

``executable``
    Carries a concrete artifact: URL/IP:port, file path, version/status+endpoint
    pair, parameter/credential, payload, or flag.

``narrative``
    A claim, plan or observation with no actionable artifact.  A banner-only
    string (``Apache/2.4.49``) is narrative here: it is witnessed, but there is
    no command to run.

``noise``
    Placeholder, protocol sentinel or environment setup: the adapter template,
    ``docker compose up -d``, ``测试环境搭建``, ``(no output)``.

The finer five-way split is retained as an **additional** diagnostic field
(:func:`classify_fact_detail`), never as a replacement: ``executable`` maps to
``executable``; ``noise`` maps to ``unknown``; ``narrative`` is split into
``observation`` / ``hypothesis`` / ``negative``.  That keeps the headline
comparable while still saying *why* a fact is not actionable.

Decision order is ``noise -> banner-only -> executable -> narrative``:

* noise first: a setup line can contain a URL or a command word and is still
  not a fact about the target.
* banner-only second: "Apache/2.4.49" matches the version signal, but with no
  other content it cannot be replayed, so it must not be executable.
* executable third: any concrete artifact wins, even if the sentence is prose.
* narrative is the residual.

Pure functions only: no IO, no model, no configuration.  Every rule is an
explicit regex so a disputed label can be traced and changed.
"""

from __future__ import annotations

import re
from typing import Iterable, Literal

FactClass = Literal["executable", "narrative", "noise"]
DetailClass = Literal["executable", "observation", "hypothesis", "negative", "unknown"]

FACT_CLASSES: tuple[str, ...] = ("executable", "narrative", "noise")
DETAIL_CLASSES: tuple[str, ...] = (
    "executable",
    "observation",
    "hypothesis",
    "negative",
    "unknown",
)

# ---------------------------------------------------------------------------
# noise: placeholders, protocol sentinels, environment setup
# ---------------------------------------------------------------------------

_NOISE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"bounded evidence-backed observation", re.IGNORECASE),
    re.compile(r"^\[(?:claude|codex)\]\s*\(no output\)\s*$", re.IGNORECASE),
    re.compile(
        r"^\[(?:claude|codex)\]\s*`?\s*NEED_(?:KIND|INPUT)=[A-Za-z_]+", re.IGNORECASE
    ),
    re.compile(r"^\[(?:claude|codex)\]\s*FOUND_FLAG=(?:NONE|none)\s*$"),
    re.compile(r'^\s*\{\s*"observation_status"\s*:\s*"UNAVAILABLE"'),
    re.compile(
        r'^\[(?:claude|codex)\]\s*\{\s*"type"\s*:\s*"turn\.(?:started|completed|failed)"\s*\}\s*$',
        re.IGNORECASE,
    ),
    re.compile(r"^\[(?:claude|codex)\]\s*$", re.IGNORECASE),
    # Environment setup is not a fact about the target.  Checked before the
    # command rules, so "docker compose up -d && curl ..." stays noise.
    re.compile(r"\bdocker\s+(?:compose|run|build|pull|exec)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:pip3?|npm|yarn|pnpm|apt(?:-get)?|apk|brew)\s+(?:install|add)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bgit\s+clone\b", re.IGNORECASE),
    re.compile(r"测试环境|环境搭建|搭建环境|启动服务|服务已启动|已启动|已运行|安装依赖|部署环境"),
    re.compile(
        r"\b(?:server|app|application|service|container)\s+(?:is\s+)?"
        r"(?:up|running|started|ready)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bcompiled successfully\b|\bready in \d+\s*ms\b", re.IGNORECASE),
)

# ---------------------------------------------------------------------------
# executable: concrete artifacts
# ---------------------------------------------------------------------------

_SIGNAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "url",
        re.compile(r"\b(?:https?|ftp|gopher|dict|file|php|data|expect)://[^\s<>'\"]+", re.IGNORECASE),
    ),
    (
        # A bare IPv4 is a target even without a port: ``curl http://10.0.0.5``.
        "ipv4",
        re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),
    ),
    (
        "host_port",
        re.compile(
            r"\b(?:\d{1,3}(?:\.\d{1,3}){3}|localhost|"
            r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}):\d{2,5}\b",
            re.IGNORECASE,
        ),
    ),
    (
        "file_path",
        # Absolute or home/traversal-rooted path with at least one segment
        # character.  The lookbehind keeps ``80/443`` and ``and/or`` out.
        re.compile(
            r"(?<![\w.>:-])(?:~|\.\.)?/(?:[A-Za-z0-9_.@+%~-]+/)*[A-Za-z0-9_.@+%~-]+"
        ),
    ),
    (
        "relative_path",
        re.compile(r"(?<![\w.-])(?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]{1,8}\b"),
    ),
    (
        # ``index.php`` without a directory.  The extension allowlist keeps
        # ordinary prose ("e.g." / "i.e.") out; every entry is requestable.
        "filename",
        re.compile(
            r"\b[\w-]+\.(?:php\d?|phtml|asp|aspx|ashx|jsp|jspx|action|do|"
            r"html?|txt|zip|tar|t?gz|xml|json|ya?ml|py|rb|pl|sh|js|"
            r"conf|config|ini|env|bak|old|swp|sql|log|db|sqlite|pem|key)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "version",
        re.compile(r"\b[A-Za-z][A-Za-z0-9_.+-]*(?:[/ ]\d+\.\d+(?:\.\d+)?)(?!\.\d)\b"),
    ),
    # Exactly three segments; lookarounds stop a match starting inside IPv4.
    ("semver", re.compile(r"(?<![\d.])\d+\.\d+\.\d+(?![\d.])")),
    (
        "parameter",
        re.compile(
            r"(?<![\w.-])(?:id|cmd|exec|command|file|filename|path|url|uri|q|query|"
            r"page|search|user|username|pass|password|passwd|pwd|token|key|api_key|"
            r"apikey|secret|debug|redirect|next|action|act|target|name|cat|type|data|"
            r"payload|host|port|email|asset_no|department|lang|template|view|include|"
            r"require|dir|folder|download|upload|ajax_form|_method|gremlin|exp|func|"
            r"function|class|code|content)\s*=(?!=)",
            re.IGNORECASE,
        ),
    ),
    (
        "array_parameter",
        re.compile(r"\b\w+\s*\[[^\]\n]{1,80}\]\s*=(?!=)"),
    ),
    (
        "credential_pair",
        re.compile(
            r"(?<![\w:/])(?!CVE-)([A-Za-z][\w.-]{1,31}):"
            r"(?=[^\s:/`'\"]{4,64}\b)(?=[^\s:/`'\"]*[\d!@#$%^&*_+=-])[^\s:/`'\"]{4,64}"
        ),
    ),
    (
        "http_header",
        re.compile(
            r"\b(?:Server|X-Powered-By|Content-Type|Content-Length|Location|"
            r"Set-Cookie|Allow|WWW-Authenticate)\s*:\s*\S+",
            re.IGNORECASE,
        ),
    ),
    (
        "credential",
        re.compile(
            r"\b(?:Cookie|Authorization|Set-Cookie)\s*:|"
            r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}|"
            r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
            re.IGNORECASE,
        ),
    ),
    (
        "payload",
        re.compile(
            r"(?<!\.)\.\./|php://|file://|gopher://|dict://|data://|expect://|\{\{|\$\{|"
            r"%00|%2e%2e|UNION\s+SELECT|\bOR\s+['\"]?1['\"]?\s*=\s*['\"]?1|"
            r"SLEEP\s*\(|WAITFOR\s+DELAY|\b__proto__\b|\$\{jndi:|base64-encode|"
            r"java\.lang\.Runtime|ProcessBuilder|O:\d+:\"|a:\d+:\{|system\(|"
            r"shell_exec|passthru|\beval\(|\bassert\(|updatexml\s*\(|"
            r"extractvalue\s*\(|into\s+outfile|load_file\s*\(|information_schema",
            re.IGNORECASE,
        ),
    ),
    (
        # ``CTF2{...}``, ``flag{...}``, ``n1book{...}`` -- a prefix is required
        # so JSON objects and ``{{...}}`` templates do not match.
        "flag",
        re.compile(r"\b[A-Za-z][A-Za-z0-9_]{1,31}\{[^{}\s]{3,200}\}"),
    ),
)

_STATUS_CODE = re.compile(
    r"\b(?:status(?:_?code)?|HTTP/\d(?:\.\d)?|returns?|responds?(?:\s+with)?|got)"
    r"\s*[\"\']?\s*[:=]?\s*(\d{3})\b",
    re.IGNORECASE,
)
_ENDPOINT_HINT = re.compile(
    r"\b(?:endpoint|path|route)\b|(?<![\w.>:-])/[A-Za-z0-9_.?=&%~-]+", re.IGNORECASE
)

# ---------------------------------------------------------------------------
# banner-only: witnessed but not replayable
# ---------------------------------------------------------------------------

_BANNER_LABEL = re.compile(r"\b(?:Server|X-Powered-By)\s*:\s*", re.IGNORECASE)
_BANNER_TOKEN = re.compile(
    r"\b[A-Za-z][A-Za-z0-9_.+-]*[/ ]\d+\.\d+(?:\.\d+)?(?!\.\d)\b|"
    r"(?<![\d.])\d+\.\d+\.\d+(?![\d.])"
)
_BANNER_REMAINDER = re.compile(r"[\s,;:/()\[\]{}._+-]+")


def _is_banner_only(text: str) -> bool:
    """True when the whole string is a banner/version and nothing else.

    ``Apache/2.4.49`` and ``Server: nginx/1.18.0`` are banner-only.  A sentence
    that happens to quote a version ("phpMyAdmin 4.8.1 on Apache/2.4.25") is
    not: it still has prose and stays executable under the normal rules.
    """

    if not _BANNER_TOKEN.search(text):
        return False
    stripped = _BANNER_LABEL.sub(" ", text)
    stripped = _BANNER_TOKEN.sub(" ", stripped)
    return not _BANNER_REMAINDER.sub("", stripped)


# ---------------------------------------------------------------------------
# detail classes for narrative: observation / hypothesis / negative
# ---------------------------------------------------------------------------

_NEGATIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bDEADEND\s*=|\bdead[\s_-]?end\b", re.IGNORECASE),
    re.compile(
        r"\bnot\s+(?:vulnerable|exploitable|reachable|accessible|possible|found|usable)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bno\s+(?:flag|result|output|response|vulnerability|leak|hit|usable)\b", re.IGNORECASE),
    re.compile(r"\bnever\s+(?:accepted|responded|returned|worked|connected)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:failed|fails|failure|refused|unreachable|timed?\s*out|blocked|disabled|"
        r"can(?:no|')t|won'?t|doesn'?t\s+work|not\s+work(?:ing)?)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b[45]\d{2}\b[^.\n]{0,40}\b(?:for\s+)?(?:every|all)\b", re.IGNORECASE),
    re.compile(r"失败|不可用|未找到|无法|拒绝|超时|死路|无效|不存在|没有成功"),
)

_OBSERVATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b[A-Za-z][A-Za-z0-9_.+-]*[/ ]\d+\.\d+(?:\.\d+)?(?!\.\d)\b"),
    re.compile(r"(?<![\d.])\d+\.\d+\.\d+(?![\d.])"),
    re.compile(
        r"\b(?:Server|X-Powered-By|Content-Type|Content-Length|Location|"
        r"Set-Cookie|Allow|WWW-Authenticate)\s*:\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:status(?:_?code)?|HTTP/\d(?:\.\d)?|returns?|responds?(?:\s+with)?|got)"
        r"\s*[\"']?\s*[:=]?\s*[1-5]\d{2}\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?<![\w.-])/[A-Za-z0-9_.?=&%~/-]{2,}"),
    re.compile(
        r"\b[\w-]+\.(?:php\d?|phtml|asp|aspx|ashx|jsp|jspx|action|do|html?|txt|"
        r"zip|tar|t?gz|xml|json|ya?ml|py|rb|pl|sh|js|conf|config|ini|env|bak|"
        r"old|swp|sql|log|db|sqlite|pem|key)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:table|tables|columns?|schema|database|db)\b", re.IGNORECASE),
    re.compile(r"\b(?:source|source code)\b|源码|代码", re.IGNORECASE),
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),
)

_HYPOTHESIS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:likely|probably|possibly|maybe|perhaps|appears?|seems?|suggests?|"
        r"might|could|suspect|assume|think|guess|should\s+be|may\s+be|plan(?:ning)?|"
        r"next\s+step|try(?:ing)?|attempt(?:ing)?|ready\s+to|going\s+to)\b",
        re.IGNORECASE,
    ),
    re.compile(r"可能|推测|猜测|疑似|尝试|计划|应该"),
)


def _normalise(content: object) -> str:
    return " ".join(str(content if content is not None else "").split())


def _matches(patterns: Iterable[re.Pattern[str]], text: str) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def _executable_signals(text: str) -> tuple[str, ...]:
    names = [name for name, pattern in _SIGNAL_PATTERNS if pattern.search(text)]
    if _STATUS_CODE.search(text) and _ENDPOINT_HINT.search(text):
        names.append("status_endpoint")
    return tuple(names)


def is_executable_fact(content: str) -> bool:
    """True only when the fact carries an artifact a command can use."""

    return classify_fact(content) == "executable"


def classify_fact(content: str) -> FactClass:
    """Return the established three-class label.

    Empty strings are ``noise`` (a broken record, not a claim).  Banner-only
    strings are ``narrative`` and never executable.
    """

    text = _normalise(content)
    if not text or _matches(_NOISE_PATTERNS, text):
        return "noise"
    if _is_banner_only(text):
        return "narrative"
    if _executable_signals(text):
        return "executable"
    return "narrative"


def classify_fact_detail(content: str) -> DetailClass:
    """Return the finer five-way label **in addition to** the three-class one.

    Mapping: ``executable -> executable``, ``noise -> unknown``, and narrative
    ``-> observation | hypothesis | negative``.  This field is diagnostic; the
    headline always uses :func:`classify_fact`.
    """

    label = classify_fact(content)
    if label == "executable":
        return "executable"
    if label == "noise":
        return "unknown"

    text = _normalise(content)
    if _matches(_NEGATIVE_PATTERNS, text):
        return "negative"
    if _matches(_OBSERVATION_PATTERNS, text):
        return "observation"
    return "hypothesis"


def executable_fact_summary(facts: list[str]) -> dict[str, object]:
    """Count the three classes and the rates the metric quotes.

    ``executable_per_100`` is per 100 *facts*, not per 100 commands; the caller
    that knows its command count divides by that instead.  ``by_detail`` is the
    additional five-way split; it sums to ``total`` and never replaces
    ``counts``.
    """

    if isinstance(facts, str):  # a bare string is one fact, not char iterable
        facts = [facts]

    counts: dict[str, int] = {name: 0 for name in FACT_CLASSES}
    by_detail: dict[str, int] = {name: 0 for name in DETAIL_CLASSES}
    total = 0
    for fact in facts:
        counts[classify_fact(fact)] += 1
        by_detail[classify_fact_detail(fact)] += 1
        total += 1

    non_noise = total - counts["noise"]

    def share(numerator: int, denominator: int) -> float:
        return round(numerator / denominator, 4) if denominator else 0.0

    return {
        "total": total,
        "counts": counts,
        "shares": {name: share(count, total) for name, count in counts.items()},
        "executable_per_100": round(counts["executable"] * 100 / total, 2) if total else 0.0,
        "non_noise": non_noise,
        "executable_share_of_non_noise": share(counts["executable"], non_noise),
        "by_detail": by_detail,
    }


__all__ = [
    "DETAIL_CLASSES",
    "FACT_CLASSES",
    "DetailClass",
    "FactClass",
    "classify_fact",
    "classify_fact_detail",
    "executable_fact_summary",
    "is_executable_fact",
]
