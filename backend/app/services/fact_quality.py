"""Classify a board fact into one of five roles.

The old metric counted every published fact.  That rewards text which cannot
be acted on: a template placeholder, a banner, a paraphrase, a dead end.  The
classification below separates what a fact *is* so the headline can count only
the ones a later agent can run or replay verbatim:

``executable``
    Carries a command line, a URL + method + payload, a payload/param set, a
    path that is read from the target, credentials recovered from output, or a
    captured flag.  Something can be sent as-is (modulo the target host).

``observation``
    A concrete but non-actionable artifact: banner/version, status code,
    response header, endpoint listing, file listing, DB schema.  Useful, but
    replaying it does not test a hypothesis.

``hypothesis``
    A claim, paraphrase or plan with no witness: "the app uses JWT",
    "probably SQLi in id", "next we try SSRF".  This includes the old
    "narrative" bucket.

``negative``
    A dead end or a negative result: "DEADEND=...", "not vulnerable",
    "port refused", "no flag found".  Negative evidence is valuable, but it
    must not inflate the executable count, even when it quotes a payload.

``unknown``
    Empty / placeholder / protocol sentinel / environment setup.  The two
    traps this module exists to keep out of ``executable`` live here:
    ``"测试环境搭建"`` and ``"docker compose up -d"`` (setup text) and the
    adapter template ``"Native Worker completed a bounded evidence-backed
    observation"``.

Definition versions
-------------------
``EXECUTABLE_DEFINITION_VERSION = "v2"`` is the strict rule used by the
headline: version/banner-only and status-only text is *not* executable.
The previous broad rule is frozen as ``v1`` and exposed as
:func:`is_executable_fact_legacy` / ``executable_legacy`` in the summary **only
so a report can print both numbers side by side**.  v1 counted any concrete
artifact -- including a version banner -- as executable; that is exactly the
2.1x gap the reconciliation flag exists to explain.

Decision order is ``unknown -> negative -> executable -> observation ->
hypothesis``:

* unknown first, because a placeholder or setup line can contain a URL or a
  command word; it is still not a fact about the target.
* negative second, because a dead end that quotes ``id=1`` is a warning not to
  retry, not a command to run.  Its role is the label.
* executable before observation, because "GET /x?id=1" is both a request and a
  response observation; the presence of a payload is what makes it testable.
* observation before hypothesis, because a version string is witnessed even
  when the sentence hedges ("the server appears to be Apache/2.4.49").
* hypothesis is the residual: a claim with no artifact.

Pure functions only: no IO, no model, no configuration.  Every rule is an
explicit regex so a disputed label can be traced and changed.
"""

from __future__ import annotations

import re
from typing import Iterable, Literal

#: Strict head-line rule.  Bump only with a deliberate, documented change;
#: the script prints this next to every ratio.
EXECUTABLE_DEFINITION_VERSION = "v2"

#: Frozen broad rule, kept solely for reconciliation.
LEGACY_EXECUTABLE_DEFINITION_VERSION = "v1"

FactClass = Literal["executable", "observation", "hypothesis", "negative", "unknown"]

FACT_CLASSES: tuple[str, ...] = (
    "executable",
    "observation",
    "hypothesis",
    "negative",
    "unknown",
)

# ---------------------------------------------------------------------------
# unknown: placeholders, protocol sentinels, environment setup
# ---------------------------------------------------------------------------

_UNKNOWN_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Adapter fallback when a step yielded no extractable fact.
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
    # Environment setup is not a fact about the target.  Kept before the
    # command rule so "docker compose up -d" never becomes executable.
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
# negative: dead ends and negative results
# ---------------------------------------------------------------------------

_NEGATIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bDEADEND\s*=|\bdead[\s_-]?end\b", re.IGNORECASE),
    re.compile(
        r"\bnot\s+(?:vulnerable|exploitable|reachable|accessible|possible|found|usable)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bno\s+(?:flag|result|output|response|vulnerability|leak|hit|usable)\b", re.IGNORECASE),
    re.compile(
        r"\bnever\s+(?:accepted|responded|returned|worked|connected)\b", re.IGNORECASE
    ),
    re.compile(
        r"\b(?:failed|fails|failure|refused|unreachable|timed?\s*out|blocked|disabled|"
        r"can(?:no|')t|won'?t|doesn'?t\s+work|not\s+work(?:ing)?)\b",
        re.IGNORECASE,
    ),
    # "404 for every id" is a negative result, not an endpoint observation.
    re.compile(r"\b[45]\d{2}\b[^.\n]{0,40}\b(?:for\s+)?(?:every|all)\b", re.IGNORECASE),
    re.compile(r"失败|不可用|未找到|无法|拒绝|超时|死路|无效|不存在|没有成功"),
)

# ---------------------------------------------------------------------------
# executable v2: command lines, requests, payloads, target paths, creds, flags
# ---------------------------------------------------------------------------

#: Tools whose presence plus an argument means "this is a command line".
#: ``docker``/``pip``/``git`` are deliberately absent: they are setup tools,
#: and their lines are already routed to ``unknown``.
_TOOL = (
    r"(?:curl|wget|nmap|sqlmap|gobuster|ffuf|feroxbuster|nuclei|nikto|wfuzz|"
    r"hydra|ssh|scp|sftp|nc|ncat|socat|python3?|bash|sh|zsh|perl|ruby|php|"
    r"mysql|psql|redis-cli|mongosh|msfconsole|searchsploit)"
)

_COMMAND_PATTERNS: tuple[re.Pattern[str], ...] = (
    # A backticked command with at least one argument: `curl -s http://...`.
    re.compile(r"`\s*(?:sudo\s+)?" + _TOOL + r"\b[^`\n]{1,300}`", re.IGNORECASE),
    # Backticked basic shell commands: `id`, `printenv FLAG`, `cat /etc/passwd`.
    # These are too common in prose to accept unquoted, but inside backticks they
    # are commands the worker actually ran (or quoted from output).
    re.compile(
        r"`\s*(?:id|whoami|hostname|uname|cat|ls|env|printenv|pwd|ifconfig|ip|ps|"
        r"find|grep|sed|awk|echo|nc)\b[^`\n]{0,300}`",
        re.IGNORECASE,
    ),
    # A shell transcript line: "$ nmap -p- host".
    re.compile(r"^\s*[$#>]\s*(?:sudo\s+)?" + _TOOL + r"\b", re.IGNORECASE),
    # A bare command with a flag, URL, host or filename argument:
    # "ssh -p 26877 -i id_rsa git@target", "sqlmap -u http://...".
    # A plain version after a product name ("MySQL 5.5.62") must not match,
    # so a filename argument has to contain a letter.
    re.compile(
        r"\b" + _TOOL + r"\s+(?:-{1,2}\S+|https?://\S+|"
        r"(?=[\w./-]*[A-Za-z])[\w./-]*\.\w+|\S+@\S+)",
        re.IGNORECASE,
    ),
)

_HTTP_METHOD = re.compile(r"\b(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b")
_URL_OR_ENDPOINT = re.compile(
    r"\bhttps?://\S+|(?<![\w.-])/[A-Za-z0-9_.?=&%~/-]{2,}", re.IGNORECASE
)

#: Payload grammar.  Kept narrow on purpose: naming a vulnerability class
#: ("prototype pollution") is a hypothesis, shipping the string is executable.
_PAYLOAD_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<!\.)\.\./"),
    re.compile(r"\b(?:php|file|gopher|dict|data|expect)://", re.IGNORECASE),
    re.compile(r"\{\{|\$\{"),
    re.compile(
        r"\bUNION\s+SELECT\b|\bSLEEP\s*\(|\bWAITFOR\s+DELAY\b|"
        r"\bupdatexml\s*\(|\bextractvalue\s*\(|\binto\s+outfile\b|\bload_file\s*\(",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:java\.lang\.)?Runtime\s*@?\.?\s*getRuntime\s*\(|"
        r"\bnew\s+ProcessBuilder\s*\(|\bProcessBuilder\s*\(",
        re.IGNORECASE,
    ),
    re.compile(r"\b__proto__\s*(?:\[|=)|\$\{jndi:", re.IGNORECASE),
    re.compile(r"\bO:\d+:\"|\ba:\d+:\{"),
    re.compile(r";\s*(?:id|whoami|cat|ls|uname|curl|wget)\b|\|\s*(?:id|whoami|cat|ls|uname)\b"),
    re.compile(r"<script\b|\bonerror\s*=", re.IGNORECASE),
    re.compile(r"%00|%2e%2e", re.IGNORECASE),
    # Require an argument: "system()" in prose is a technique name, while
    # "system('env')" or "system($_GET[cmd])" is a payload to send.
    re.compile(
        r"\b(?:system|shell_exec|passthru|eval|assert)\s*\(\s*[\"'$]",
        re.IGNORECASE,
    ),
)

#: Parameter names that carry an attack surface.  A value is required and a
#: bare ``status=1`` (an ordinary response field) is not enough.
_SENSITIVE_PARAM = re.compile(
    r"(?<![\w.-])(?:id|cmd|exec|command|file|filename|path|url|uri|q|query|page|"
    r"search|user|username|pass|password|passwd|pwd|token|key|api_key|apikey|"
    r"secret|debug|redirect|next|action|act|target|name|cat|type|data|payload|"
    r"host|port|email|asset_no|department|lang|template|view|include|require|"
    r"dir|folder|download|upload|ajax_form|_method|gremlin|exp|func|function|"
    r"class|code|content)\s*=(?!=)\S{1,200}",
    re.IGNORECASE,
)
_JSON_REQUEST_BODY = re.compile(
    r"\b(?:with|body|payload|data)\s*[:=]?\s*[\{\[]", re.IGNORECASE
)

#: A file that is read from the target, not a local helper.  ``/home/kali``
#: and ``/usr/share`` are this project's own workspace/tool paths.
_TARGET_PATH = re.compile(
    r"(?<![\w.-])/(?:etc|proc|var(?!log/)|flag\b|opt|srv|app|root|"
    r"usr/local)(?:/[\w.@+%~-]+)*",
    re.IGNORECASE,
)

_COOKIE = re.compile(r"\b(?:Cookie|Authorization|Set-Cookie)\s*:\s*\S+", re.IGNORECASE)
_BEARER = re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_CREDENTIAL_PAIR = re.compile(
    r"(?<![\w:/])(?!CVE-)([A-Za-z][\w.-]{1,31}):"
    r"(?=[^\s:/`'\"]{3,64}\b)[^\s:/`'\"]{3,64}"
)
_SUCCESS_MARKER = re.compile(
    r"\b(?:valid|works?|working|succeeds?|success(?:ful)?|accepted|granted|"
    r"authenticated|logs?\s+in|logged?\s*in|login\s+(?:ok|success))\b|"
    r"成功|有效|登录成功",
    re.IGNORECASE,
)

_FLAG = re.compile(r"\b[A-Za-z][A-Za-z0-9_]{1,31}\{[^{}\s]{3,200}\}")

# ---------------------------------------------------------------------------
# observation: witnessed but non-actionable
# ---------------------------------------------------------------------------

_OBSERVATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Name + version, or a standalone three-part version.  This is the trap:
    # "Apache/2.4.49" is observation, never executable.
    re.compile(r"\b[A-Za-z][A-Za-z0-9_.+-]*(?:[/ ]\d+\.\d+(?:\.\d+)?)(?!\.\d)\b"),
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
    # Endpoint/path listing without a payload.
    re.compile(r"(?<![\w.-])/[A-Za-z0-9_.?=&%~/-]{2,}"),
    # A named artifact on disk (source archive, page, config) without a
    # command that reads it from the target.
    re.compile(
        r"\b[\w-]+\.(?:php\d?|phtml|asp|aspx|ashx|jsp|jspx|action|do|html?|txt|"
        r"zip|tar|t?gz|xml|json|ya?ml|py|rb|pl|sh|js|conf|config|ini|env|bak|"
        r"old|swp|sql|log|db|sqlite|pem|key)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:table|tables|columns?|schema|database|db)\b", re.IGNORECASE),
    re.compile(r"\b(?:source|source code)\b|源码|代码", re.IGNORECASE),
    # A bare IPv4 with no port/request is a host observation.
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),
)

# ---------------------------------------------------------------------------
# hypothesis: claims and plans without a witness
# ---------------------------------------------------------------------------

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


def _is_request_with_payload(text: str) -> bool:
    if not (_HTTP_METHOD.search(text) and _URL_OR_ENDPOINT.search(text)):
        return False
    return bool(
        _matches(_PAYLOAD_PATTERNS, text)
        or _SENSITIVE_PARAM.search(text)
        or _JSON_REQUEST_BODY.search(text)
    )


def _has_credentials(text: str) -> bool:
    if _COOKIE.search(text) or _BEARER.search(text) or _JWT.search(text):
        return True
    # The success marker has to sit next to the pair; "CVE-x: y works" in a
    # different clause must not turn a CVE id into a credential.
    for match in _CREDENTIAL_PAIR.finditer(text):
        window = text[max(0, match.start() - 80) : match.end() + 80]
        if _SUCCESS_MARKER.search(window):
            return True
    return False


def is_executable_fact(content: str) -> bool:
    """True only when the fact can be run or replayed verbatim (v2 rule).

    A banner, a paraphrase and an echo without a witness are all False.
    """

    return classify_fact(content) == "executable"


def classify_fact(content: str) -> str:
    """Return one of :data:`FACT_CLASSES` for one fact string (v2 rule)."""

    text = _normalise(content)
    if not text or _matches(_UNKNOWN_PATTERNS, text):
        return "unknown"
    if _matches(_NEGATIVE_PATTERNS, text):
        return "negative"

    if (
        _matches(_COMMAND_PATTERNS, text)
        or _is_request_with_payload(text)
        or _matches(_PAYLOAD_PATTERNS, text)
        or _TARGET_PATH.search(text)
        or _has_credentials(text)
        or _FLAG.search(text)
    ):
        return "executable"

    if _matches(_OBSERVATION_PATTERNS, text):
        return "observation"
    if _matches(_HYPOTHESIS_PATTERNS, text):
        return "hypothesis"
    return "hypothesis"


# ---------------------------------------------------------------------------
# v1 legacy rule (frozen): kept only for reconciliation
# ---------------------------------------------------------------------------

#: The broad rule used before the v2 split.  It is intentionally duplicated and
#: frozen: reconciliation is only meaningful if it applies the *old* rule, not
#: a re-interpretation of it.  Do not extend this tuple.
_LEGACY_SIGNAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "url",
        re.compile(r"\b(?:https?|ftp|gopher|dict|file|php|data|expect)://[^\s<>'\"]+", re.IGNORECASE),
    ),
    (
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
        re.compile(
            r"(?<![\w.>:-])(?:~|\.\.)?/(?:[A-Za-z0-9_.@+%~-]+/)*[A-Za-z0-9_.@+%~-]+"
        ),
    ),
    (
        "relative_path",
        re.compile(r"(?<![\w.-])(?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]{1,8}\b"),
    ),
    (
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
        "flag",
        re.compile(r"\b[A-Za-z][A-Za-z0-9_]{1,31}\{[^{}\s]{3,200}\}"),
    ),
)

_LEGACY_STATUS_CODE = re.compile(
    r"\b(?:status(?:_?code)?|HTTP/\d(?:\.\d)?|returns?|responds?(?:\s+with)?|got)"
    r"\s*[\"\']?\s*[:=]?\s*(\d{3})\b",
    re.IGNORECASE,
)
_LEGACY_ENDPOINT_HINT = re.compile(
    r"\b(?:endpoint|path|route)\b|(?<![\w.>:-])/[A-Za-z0-9_.?=&%~-]+", re.IGNORECASE
)

_BANNER_LABEL = re.compile(r"\b(?:Server|X-Powered-By)\s*:\s*", re.IGNORECASE)
_BANNER_TOKEN = re.compile(
    r"\b[A-Za-z][A-Za-z0-9_.+-]*[/ ]\d+\.\d+(?:\.\d+)?(?!\.\d)\b|"
    r"(?<![\d.])\d+\.\d+\.\d+(?![\d.])"
)
_BANNER_REMAINDER = re.compile(r"[\s,;:/()\[\]{}._+-]+")


def _is_banner_only(text: str) -> bool:
    """True when the whole string is a banner/version and nothing else."""

    if not _BANNER_TOKEN.search(text):
        return False
    stripped = _BANNER_LABEL.sub(" ", text)
    stripped = _BANNER_TOKEN.sub(" ", stripped)
    return not _BANNER_REMAINDER.sub("", stripped)


def is_executable_fact_legacy(content: str) -> bool:
    """Apply the frozen v1 broad rule (reconciliation only).

    It counts any concrete artifact, including a version banner or a bare
    status+endpoint pair, so it returns more executable facts than v2.
    """

    text = _normalise(content)
    if not text or _matches(_UNKNOWN_PATTERNS, text) or _is_banner_only(text):
        return False
    if any(pattern.search(text) for _, pattern in _LEGACY_SIGNAL_PATTERNS):
        return True
    return bool(_LEGACY_STATUS_CODE.search(text) and _LEGACY_ENDPOINT_HINT.search(text))


def classify_fact_legacy(content: str) -> str:
    """Return ``executable`` / ``narrative`` / ``noise`` under the v1 rule."""

    text = _normalise(content)
    if not text or _matches(_UNKNOWN_PATTERNS, text):
        return "noise"
    return "executable" if is_executable_fact_legacy(text) else "narrative"


def executable_fact_summary(facts: list[str]) -> dict[str, object]:
    """Count v2 classes and carry the v1 number alongside them.

    ``executable`` / ``ratio`` / ``by_class`` are v2.  ``executable_legacy`` /
    ``legacy_ratio`` are the frozen v1 rule on the same facts, so a report can
    never present the new number as if it were the old one.  ``moved_from_legacy``
    is ``executable_legacy - executable`` (facts the stricter rule downgraded).
    """

    if isinstance(facts, str):  # tolerate a single string, not a char iterable
        facts = [facts]

    by_class: dict[str, int] = {name: 0 for name in FACT_CLASSES}
    total = 0
    legacy_executable = 0
    for fact in facts:
        by_class[classify_fact(fact)] += 1
        legacy_executable += int(is_executable_fact_legacy(fact))
        total += 1

    def ratio(value: int) -> float:
        return round(value / total, 4) if total else 0.0

    return {
        "total": total,
        "executable": by_class["executable"],
        "ratio": ratio(by_class["executable"]),
        "by_class": by_class,
        "definition_version": EXECUTABLE_DEFINITION_VERSION,
        "executable_legacy": legacy_executable,
        "legacy_ratio": ratio(legacy_executable),
        "legacy_definition_version": LEGACY_EXECUTABLE_DEFINITION_VERSION,
        "moved_from_legacy": legacy_executable - by_class["executable"],
    }


__all__ = [
    "EXECUTABLE_DEFINITION_VERSION",
    "FACT_CLASSES",
    "FactClass",
    "LEGACY_EXECUTABLE_DEFINITION_VERSION",
    "classify_fact",
    "classify_fact_legacy",
    "executable_fact_summary",
    "is_executable_fact",
    "is_executable_fact_legacy",
]
