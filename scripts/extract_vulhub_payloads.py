"""Turn the vendored vulhub corpus into executable playbook entries.

Why this exists, precisely: ``data/knowledge/vulhub.jsonl`` already had 151
entries pointing at these READMEs, but **121 of them carried a one-line
``payload_template``** -- ``POST /geoserver/TestWfsPost`` -- with the real
request left behind the sentence "full request in README".  A Worker that
retrieves such an entry learns an endpoint and nothing else, which is exactly
the failure mode measured in ``docs/notes/2026-11/2026-11-05-failure-attribution.md``: 87
commands per Run, 4 facts, because every attempt re-invents the payload.

The READMEs do contain the real thing.  For GeoServer CVE-2021-40822 the
``## 漏洞复现`` section carries a complete ``POST /geoserver/TestWfsPost``
request with ``url=...&body=...&username=admin``.  That request is what belongs
in the playbook, so this extractor lifts the fenced code blocks out of the
"reproduce" section and keeps the ones that are actually runnable.

Design decisions that cost real decisions to make:

* **Prefer the Chinese README.**  Both languages exist (661 files for 246 CVEs);
  the ``zh-cn`` variant is the upstream-maintained one for this corpus and the
  English file is sometimes a stub.  Only one is used per CVE, never both, or
  every entry would be duplicated.
* **A request must have a request line.**  ``GET /path HTTP/1.1`` or a
  ``curl``/``python`` invocation.  A fenced block that is just ``docker compose
  up -d`` is setup, not exploitation -- including it would teach the Worker to
  install the target, the exact wasted-work pattern this project keeps fixing.
* **Filesystem and credential values become placeholders.**  ``your-ip``,
  ``127.0.0.1``, ``localhost``, and the README's own ``admin:admin`` pairs are
  per-instance facts; leaving them literal makes an entry that looks valid and
  cannot run.
* **Never mark an import ``verified``.**  Nothing here has been fired at the
  arena.  Trust stays ``unverified`` so the matcher may show the entry but may
  not let it issue a FIRST ACTION.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = ROOT / "data" / "sources" / "vulhub"
DEFAULT_OUT = ROOT / "data" / "knowledge" / "candidates" / "vulhub-payloads.jsonl"

#: A fenced block is a candidate payload if it opens with something that can be
#: sent.  Deliberately narrow: a block that only mentions a filename teaches
#: nothing about how to reach the vulnerability.
#:
#: The trailing ``|`` used to make this pattern match **everything** -- an empty
#: alternative matches at position 0 of any string, so
#: ``_RUNNABLE.search("junk")`` was True.  That silently turned the "is this
#: sendable?" gate into a no-op: measured on the 248-README corpus, 132 blocks
#: passed *only* because of the empty branch, including Nginx ``location / {``,
#: Samba ``[global]``, ``#EXTM3U`` playlists and C source.  ``_SETUP`` masked
#: part of the damage (docker/git/npm lines were rejected separately), which is
#: why the defect survived: the visible symptom was "some config snippets leak
#: through", not "the gate is gone".
#:
#: ``MOVE``/``COPY`` are WebDAV verbs and appear as real request lines in
#: ActiveMQ CVE-2016-3088.  The client verbs (mysql, smbclient, redis-cli, ...)
#: are the payload for non-HTTP services -- MySQL CVE-2012-2122's only exploit
#: is a ``mysql`` login loop, and dropping them would lose a real payload.
_RUNNABLE = re.compile(
    r"^\s*(?:"
    r"(?:GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS|MOVE|COPY)\s+\S+"  # raw HTTP request
    r"|curl\s"                                                     # curl one-liner
    r"|(?:python3?|java|bash|sh|zsh|pwsh|powershell|ruby|perl|php|node"
    r"|mysql|smbclient|sqlmap|nmap|ssh|redis-cli|nc|ncat|socat"
    r"|for\s+i\s+in)\s+\S"                                         # a client invocation
    r"|POC_DATA"
    r")",
    re.I,
)

#: Blocks that are infrastructure, not exploitation.  Seen constantly:
#: ``docker compose up -d``, ``docker-compose exec ... ``, image builds.
_SETUP = re.compile(
    r"^\s*(?:docker\s*(?:compose|-compose)?\s+(?:up|down|build|pull|exec|run)\b|"
    r"git\s+clone\b|npm\s+(?:i|install)\b|pip\s+install\b|mvn\b|gradle\b|"
    r"make\b|cd\s+\S+\s*$)",
    re.I,
)

#: ``## 漏洞复现`` / ``## Reproduce`` / ``## Exploit`` / ``## Vulnerability
#: Reproduction`` -- the sections that hold the actual attack, as opposed to
#: background or remediation.
#:
#: Three shapes exist in this corpus and all must match:
#:   ``## 漏洞复现`` followed directly by code blocks,
#:   ``## 漏洞详情`` followed by ``### 写入 webshell`` / ``### 写入 crontab``, and
#:   ``## Vulnerability Reproduction`` -- a keyword with a modifier in front.
#:
#: The third shape was missed on the first pass because the pattern required the
#: keyword to occupy the whole heading.  It cost five CVEs outright (Apache
#: CVE-2017-15715, GhostScript CVE-2019-6116, Drupal CVE-2019-6341 among them)
#: even though their READMEs were present and their payloads were ordinary
#: requests -- exactly the shape this extractor exists to find.
#:
#: The trailing keyword is what decides: ``Vulnerability Detail`` is a
#: reproduction section, while ``Vulnerability Reproduction`` is too, and the
#: bare document title (``# Samba Authenticated RCE (CVE-2017-7494)``) is *not*,
#: which is why the keyword must be anchored at the end of the heading rather
#: than merely appear somewhere in it.
_REPRO_SECTION = re.compile(
    r"^#{1,4}\s*(?:"
    # Bare keyword headings.  ``测试过程`` is the zh-cn spelling Samba
    # CVE-2017-7494 uses; ``复现漏洞`` is the reversed word order that six
    # others use.  Both mean "reproduce", neither is "测试环境搭建" (setup).
    r"漏洞复现|漏洞利用|漏洞详情|漏洞验证|复现|利用|攻击|验证|测试过程|复现漏洞|"
    r"reproduc\w*|exploit\w*|poc|proof of concept|usage|attack|"
    # Same keywords with a leading modifier: ``Vulnerability Reproduction``,
    # ``Exploitation Details``, ``漏洞复现步骤``.
    r"(?:vulnerabilit(?:y|ies)|vuln|bug|issue|exploit\w*|attack|poc|漏洞|攻击|利用|复现)"
    r"[\s\-_]*(?:reproduc\w*|exploit\w*|detail\w*|poc|proof of concept|steps?|"
    r"复现\w*|利用\w*|详情|步骤|过程)"
    r")\s*[:：]?\s*$",
    re.I,
)

#: Inline code that carries a full request.  Several READMEs never use a fenced
#: block at all -- phpMyAdmin CVE-2018-12613's entire reproduce section is prose
#: with ``http://your-ip:8080/index.php?target=db_sql.php%253f/../../../etc/passwd``
#: backticked mid-sentence.  Without this path that CVE yields nothing, despite
#: being one of the most-reused recipes in the curated corpus.
#:
#: The ``?`` is not required.  Apache CVE-2017-15715's payload is
#: ``http://192.168.174.128:8080/shell.php%0a`` -- a bare URL whose whole trick
#: is the trailing newline -- and requiring a query string dropped it.
_INLINE_REQUEST = re.compile(
    r"`(https?://[^\s`]+)`",
    re.I,
)

#: Inline *script invocations*, the other common prose form:
#: ``调用 `python3 poc.py http://your-ip:8080/`，读取返回结果``
#: (nginx CVE-2017-7529, Tomcat Ghostcat).  The script itself ships in the same
#: directory, so the invocation *is* the payload for anyone who has that tree --
#: and the Worker does, at ``/home/kali/pocs/vulhub``.
_INLINE_SCRIPT = re.compile(
    r"`((?:python3?|java|bash|sh|ruby|perl)\s+[^`\n]*?\.(?:py|jar|sh|rb|pl)\b[^`\n]*)`",
    re.I,
)

#: Inline *bare paths* that are themselves the bypass:
#: ``构造恶意请求 `/xxx/..;/admin/`` `` (Shiro CVE-2020-1957).  There is no host,
#: no method and no body -- the whole vulnerability is the path shape, and the
#: Worker supplies the target.  Matched only when a traversal or semicolon
#: segment is present, or every inline path in every README would qualify.
_INLINE_PATH = re.compile(r"`(/[^`\s]*(?:\.\.;|\.\./|%2e|;)[^`\s]*)`", re.I)

#: A block that only *demonstrates* the bug rather than exploiting it: an XSS
#: probe (``<script>alert(1)</script>``) proves script execution to a human
#: reading a browser, and a Worker reading a response body learns nothing from
#: it.  Measured 2026-11-06: 3 of 480 entries were this, including
#: ``vulhub:django:cve-2017-12794`` -- the entry a box titled with that CVE
#: retrieves, so the Worker's one hint was a popup it could not observe.
#:
#: ``touch /tmp/success`` is deliberately NOT filtered: it is a complete exploit
#: that chooses a harmless payload, and changing the command to read the flag is
#: a one-token edit the Worker can make.
#:
#: A block that only *demonstrates* the bug rather than exploiting it: an XSS
#: probe (``<script>alert(1)</script>``) proves script execution to a human
#: reading a browser, and a Worker reading a response body learns nothing from
#: it.  Measured 2026-11-06: 3 of 480 entries were this, including
#: ``vulhub:django:cve-2017-12794`` -- the entry a box titled with that CVE
#: retrieves, so the Worker's one hint was a popup it could not observe.
#: Not anchored: the probe usually arrives wrapped in a request line
#: (``GET /create_user/?username=<script>alert(1)</script> HTTP/1.1``), so a
#: ``^``-anchored pattern never fires on it.  That mistake shipped once and the
#: entry stayed in the corpus.
_PROBE_ONLY = re.compile(
    r"<script[^>]*>\s*alert|(?<![a-z])alert\(\s*(?:1|document\.)",
    re.I,
)

#: Blocks that are a *transcript*, not a payload: a shell session pasted in with
#: its prompt and output (``ubuntu@ubuntu:~$ id`` / ``uid=1000(ubuntu) gid=...``).
#: A Worker cannot replay a session it did not have; it can replay the command.
_TRANSCRIPT = re.compile(
    r"^\s*[\w.-]+@[\w.-]+:[^\n]*\$\s+\S+.*\n.*\b(?:uid|gid|total|drwx|rw-r)", re.I | re.S
)

#: A *configuration excerpt*, not a payload.  Samba CVE-2017-7494's English
#: README shows the target's ``smb.conf`` directly above the exploit:
#:
#:   [global]
#:       map to guest = Bad User
#:       server string = Samba Server Version %v
#:
#: That is a description of the *target*, not something the Worker can send.
#: It passed anyway because ``_RUNNABLE`` used to match any string at all (see
#: the note there); once that was fixed it still slipped through the Nginx
#: ``location / { return 302 ... }`` shape, so the shapes are excluded on their
#: own merits rather than as a side effect of the empty-alternative bug.
#:
#: The test is deliberately narrow, because a false positive here deletes a real
#: payload: no language tag (``xml``/``json``/``sh`` blocks are payload bodies
#: for other CVEs), no URL, no command verb, and at least two ``key = value``
#: lines or one ``[section]`` header plus a ``key = value``.
_CONFIG_FRAGMENT = re.compile(r"^\s*[\w.\-\[\]\"']+\s*=\s*\S.*$")
_CONFIG_SECTION = re.compile(r"^\s*\[\w[\w \-]*\]\s*$")
_CONFIG_VERB = re.compile(
    r"\b(?:curl|wget|GET|POST|PUT|DELETE|PATCH|python3?|java|bash|sh|mysql|"
    r"smbclient|docker|git|npm|ssh|nc|sqlmap|nmap|import|require)\b",
    re.I,
)
_URLISH = re.compile(r"https?://|\{\{target_url\}\}|\{\{host\}\}", re.I)


def _is_config_fragment(language: str, body: str) -> bool:
    """Is this block a configuration excerpt rather than something to send?

    See ``_CONFIG_FRAGMENT`` for why the shape alone is not enough.
    """

    if language:
        return False
    stripped = body.strip()
    if not stripped or _URLISH.search(stripped) or _CONFIG_VERB.search(stripped):
        return False
    lines = [
        line for line in stripped.splitlines()
        if line.strip() and not line.strip().startswith(("#", ";", "//"))
    ]
    if len(lines) < 2:
        return False
    pairs = sum(1 for line in lines if _CONFIG_FRAGMENT.match(line))
    sections = sum(1 for line in lines if _CONFIG_SECTION.match(line))
    return pairs >= 2 or (sections >= 1 and pairs >= 1)

#: A captured inline URL is a payload only if it carries an attack-shaped value:
#: a traversal, a filter wrapper, a parameter that is obviously injected, or an
#: encoded byte (``%0a`` -- the whole point of CVE-2017-15715).
#:
#: Widened from "must have a query string" after that CVE was missed, and
#: therefore also made stricter elsewhere: a bare ``https://github.com/...``
#: reference must not qualify, so a *plain* URL needs an explicit port, which
#: documentation links never carry.
_INLINE_ATTACK = re.compile(
    r"(?:\.\./|%2e%2e|php://|file=|target=|cmd=|exec|select|union|\{\{|\$\{|jndi:|"
    r"\.\.%2f|%0[0-9a-f]|%2[0-9a-f]|%3[0-9a-f]|:\d{2,5}/)",
    re.I,
)

#: A script invocation plus its target is a complete payload once the target is
#: parameterised, but it collapses to ~45 chars (``python3 poc.py {{target_url}}``)
#: and would fail the generic 60-char floor.  This floor applies instead.
_SCRIPT_MIN_CHARS = 30

_FENCE = re.compile(r"^\s*```+\s*([A-Za-z0-9_+-]*)\s*$")

#: Per-instance values that must be parameterised.
#:
#: Order matters.  ``Host: your-ip:8080`` must be handled before the bare
#: ``your-ip`` rule, otherwise the port is left glued to a placeholder that
#: already contains ``host:port`` and the next Run sends to
#: ``host:8080:8080``.  Measured on the first import: 168 of 310 entries had
#: exactly that shape.
_PLACEHOLDERS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Whole host:port pairs first, in both header and URL positions.
    (re.compile(r"(https?://)(?:your-ip|target-ip|127\.0\.0\.1|localhost)(?::\d+)?", re.I),
     r"\1{{target_url}}"),
    (re.compile(r"(?im)^(\s*Host:\s*)(?:your-ip|target-ip|127\.0\.0\.1|localhost)(?::\d+)?",
                ), r"\1{{host}}"),
    # Then a bare host token, still swallowing an adjacent port so it cannot be
    # duplicated.  ``{{target_url}}`` is defined to include the port.
    (re.compile(r"\b(?:your-ip|target-ip)\b(?::\d+)?", re.I), "{{target_url}}"),
    (re.compile(r"\b(?:127\.0\.0\.1|localhost)\b(?::\d+)?", re.I), "{{target_url}}"),
    # Credentials the README ships for its own container.  They are correct for
    # vulhub's compose file and almost never for the arena box.
    #
    # The replacement carries no trailing comment: this text lands inside an
    # HTTP request, and ``Authorization: Basic X  # note`` is a malformed header
    # that no client will send.  The hint belongs in ``preconditions``.
    (re.compile(r"Basic\s+YWRtaW46YWRtaW4=", re.I), "Basic {{basic_auth}}"),
    (re.compile(r"\badmin:admin\b"), "{{username}}:{{password}}"),
    (re.compile(r"your-session-cookie", re.I), "{{session_cookie}}"),
)

#: CVE ids anywhere in the path.
_CVE = re.compile(r"(CVE-\d{4}-\d{4,})", re.I)

#: A technique label from the CVE's title line, refined by the payload shape.
_TECHNIQUE_HINTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"deserializ|反序列化|unserialize", re.I), "deserialization"),
    (re.compile(r"sql\s*inject|SQL 注入|sqli", re.I), "sqli"),
    (re.compile(r"ssrf|服务端请求伪造|服务端请求", re.I), "ssrf"),
    (re.compile(r"\bxxe\b|xml external|XML 外部实体", re.I), "xxe"),
    (re.compile(r"ssti|模板注入|template injection|OGNL|SpEL|表达式注入", re.I), "ssti"),
    (re.compile(r"path traversal|director(y|ies) traversal|目录穿越|任意文件读取|arbitrary file read|lfi", re.I), "path-traversal"),
    (re.compile(r"file upload|任意文件上传|上传", re.I), "file-upload"),
    (re.compile(r"auth(?:entication)? bypass|认证绕过|JWT", re.I), "auth-bypass"),
    (re.compile(r"\brce\b|remote code|命令执行|代码执行|command execution|command injection", re.I), "rce"),
    (re.compile(r"injection|注入", re.I), "injection"),
)


@dataclass
class Stats:
    cves: int = 0
    readmes: int = 0
    no_repro_section: int = 0
    no_runnable_block: int = 0
    setup_only: int = 0
    too_short: int = 0
    inline_recovered: int = 0
    emitted: int = 0
    by_reason: Counter = field(default_factory=Counter)


def _read(path: Path) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return ""


def _pick_readme(cve_dir: Path) -> Path | None:
    """One README per CVE: ``zh-cn`` first, then plain ``README.md``.

    Both languages ship for nearly every CVE (661 files, 246 CVEs).  Reading
    both would emit every payload twice, and the duplicate would compete with
    the original for the same retrieval slot.
    """

    for name in ("README.zh-cn.md", "README.md"):
        candidate = cve_dir / name
        if candidate.is_file():
            return candidate
    matches = sorted(cve_dir.glob("README*.md"))
    return matches[0] if matches else None


def _heading_level(line: str, *, in_fence: bool = False) -> int:
    """Heading depth, or 0 for a line inside a code fence.

    A fence containing ``# apt install smbclient`` is a shell comment, but read
    naively it is an H1 that closes the section it sits in.  Measured on Samba
    CVE-2017-7494: 14 code blocks, 0 extracted payloads, because its
    ``## 漏洞复现`` section ended two lines in at a comment.

    Tracking fences is the reliable test -- a comment mentioning ``curl`` and one
    mentioning nothing are treated identically.
    """

    if in_fence:
        return 0
    match = re.match(r"^(#{1,6})\s+\S", line)
    return len(match.group(1)) if match else 0


def _fence_map(lines: list[str]) -> set[int]:
    """Indices of every line that lives inside (or delimits) a code fence."""

    fenced: set[int] = set()
    open_fence = False
    for index, line in enumerate(lines):
        if re.match(r"^\s*```", line):
            fenced.add(index)
            open_fence = not open_fence
            continue
        if open_fence:
            fenced.add(index)
    return fenced


def _repro_sections(markdown: str) -> list[str]:
    """Text of every "reproduce/exploit" section, **including its subsections**.

    Subsections matter: ActiveMQ CVE-2016-3088 has ``## 漏洞详情`` with
    ``### 写入 webshell``, ``### 写入 crontab`` and ``### 写入 jetty.xml`` beneath
    it, and every payload is inside those.  A section parser that stops at the
    next heading of any level returns the parent's two lines of prose and finds
    nothing to exploit -- which is what happened to nine CVEs on the first pass.
    """

    lines = markdown.splitlines()
    fenced = _fence_map(lines)
    sections: list[str] = []
    current: list[str] | None = None
    section_level = 0

    for index, line in enumerate(lines):
        level = _heading_level(line, in_fence=index in fenced)
        if level:
            if current is not None and level <= section_level:
                # A heading at or above the section's own level closes it.
                sections.append("\n".join(current))
                current = None
            if current is None and _REPRO_SECTION.match(line.strip()):
                current = []
                section_level = level
                continue
        if current is not None:
            current.append(line)
    if current is not None:
        sections.append("\n".join(current))
    return [s for s in sections if s.strip()]


def _fenced_blocks(text: str) -> list[tuple[str, str]]:
    """``(language, body)`` for every fenced block in ``text``."""

    blocks: list[tuple[str, str]] = []
    language: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        fence = _FENCE.match(line)
        if fence:
            if language is None:
                language = fence.group(1) or ""
                body = []
            else:
                blocks.append((language, "\n".join(body).strip("\n")))
                language = None
                body = []
            continue
        if language is not None:
            body.append(line)
    return blocks


def _is_runnable(body: str) -> bool:
    stripped = body.strip()
    if not stripped:
        return False
    # A pasted shell session is not a payload: the command in it is, but the
    # surrounding prompt and output are the author's, not the Worker's.
    if _TRANSCRIPT.match(stripped):
        return False
    # A proof-of-concept popup is not an exploit.  ``search``, not ``match``:
    # the payload sits inside a URL, not at the start of the block.
    if _PROBE_ONLY.search(stripped):
        return False
    # A raw HTTP request may begin with a comment or a variable assignment; look
    # for a request line anywhere in the first few lines.
    head = "\n".join(stripped.splitlines()[:5])
    if _SETUP.match(stripped) and not re.search(r"curl|HTTP/1\.[01]", stripped):
        return False
    return bool(_RUNNABLE.search(head) or re.search(r"^\s*(?:GET|POST|PUT)\s+\S+\s+HTTP/", stripped, re.M))


def _parameterise(body: str) -> tuple[str, list[str]]:
    """Replace per-instance values, returning the names of what was replaced."""

    found: list[str] = []
    out = body
    for pattern, replacement in _PLACEHOLDERS:
        if pattern.search(out):
            for name in re.findall(r"\{\{(\w+)\}\}", replacement):
                found.append(name)
            out = pattern.sub(replacement, out)
    return out, sorted(set(found))


#: A bracketed parameter name (``list[fullordering]``) is the *whole point* of
#: many PHP exploits, and it is also a shell glob to curl.  Measured on run
#: ``6712279f`` (Joomla CVE-2017-8917): the Worker took this corpus's payload,
#: converted the ``GET ... HTTP/1.1`` line into a ``curl`` call, and curl
#: answered ``(3) bad range in URL position 95`` -- the first action of the run
#: was spent on a quoting problem the payload itself invited.
#:
#: The fix belongs in the payload, not in a hope that every consumer quotes
#: correctly: percent-encode the brackets.  ``list%5Bfullordering%5D`` is the
#: same parameter to PHP and inert to every shell.
_BRACKETED_PARAM = re.compile(r"(?<=\w)\[(\w+)\](?==)")


def _technique(title: str, payload: str) -> str:
    """Label from the CVE title, then corrected by what the payload actually is.

    The payload wins when the two disagree: a CVE described as "SSRF" whose
    reproduce block is a signed-parameter forgery is filed as the forgery, or it
    will never be retrieved by the axis that matches the work.
    """

    for pattern, label in _TECHNIQUE_HINTS:
        if pattern.search(title):
            return label
    lowered = payload.casefold()
    if "gremlin" in lowered:
        return "graph-query-injection"
    if "jndi:" in lowered:
        return "ssti"
    if "union" in lowered and "select" in lowered:
        return "sqli"
    return "unknown"


def _title_of(markdown: str, fallback: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def build(source: Path, stats: Stats | None = None) -> list[dict]:
    """One playbook entry per (CVE, distinct runnable block)."""

    stats = stats or Stats()
    entries: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for cve_dir in sorted(p for p in source.glob("*/*") if p.is_dir()):
        match = _CVE.search(cve_dir.name)
        if not match:
            continue
        stats.cves += 1
        cve = match.group(1).upper()
        product = cve_dir.parent.name

        readme = _pick_readme(cve_dir)
        if readme is None:
            continue
        stats.readmes += 1
        markdown = _read(readme)
        if not markdown:
            continue

        sections = _repro_sections(markdown)
        if not sections:
            stats.no_repro_section += 1
            continue

        # ``(form, text)`` -- the form decides the short-payload floor below.
        blocks: list[tuple[str, str]] = []
        for section in sections:
            for language, body in _fenced_blocks(section):
                # A configuration excerpt describes the *target*, not a request
                # the Worker can send.  See ``_is_config_fragment``.
                if _is_config_fragment(language, body):
                    continue
                if not _is_runnable(body):
                    if body.strip():
                        stats.setup_only += 1
                    continue
                blocks.append(("fence", body))
            # Second chance for READMEs that never open a fence: lift the
            # request out of inline code.  Only attack-shaped ones count, or a
            # prose mention of ``http://your-ip:8080/`` becomes a "payload".
            for match in _INLINE_REQUEST.finditer(section):
                candidate = match.group(1).strip()
                # The probe filter has to run here too: an inline URL bypasses
                # ``_is_runnable``, which is how ``.../create_user/?username=
                # <script>alert(1)</script>`` reached the corpus.
                if _INLINE_ATTACK.search(candidate) and not _PROBE_ONLY.search(candidate):
                    blocks.append((
                        "fence",
                        f"GET {candidate} HTTP/1.1\nHost: {{{{host}}}}\nConnection: close\n",
                    ))
                    stats.inline_recovered += 1
            # Same for a script invocation: the script ships beside the README,
            # so naming it with a target argument is enough to reproduce.
            for match in _INLINE_SCRIPT.finditer(section):
                candidate = match.group(1).strip()
                if re.search(r"\d|your-ip|target|127\.0\.0\.1|localhost", candidate, re.I):
                    blocks.append(("script", candidate))
                    stats.inline_recovered += 1
            # And for a bare bypass path: the path *is* the exploit.  Wrap it in
            # a request line, because ``/xxx/..;/admin/`` on its own tells the
            # Worker nothing about method or what "success" looks like, and at
            # 16 chars it would also fall under any sane length floor.
            for match in _INLINE_PATH.finditer(section):
                candidate = match.group(1).strip()
                blocks.append((
                    "fence",
                    f"GET {candidate} HTTP/1.1\nHost: {{{{host}}}}\nConnection: close\n",
                ))
                stats.inline_recovered += 1

        if not blocks:
            stats.no_runnable_block += 1
            continue

        title = _title_of(markdown, f"{product} {cve}")
        for index, (form, body) in enumerate(blocks, 1):
            payload, placeholders = _parameterise(body)
            # Percent-encode bracketed parameter names so a curl conversion
            # cannot glob-fail on them.  See ``_BRACKETED_PARAM``.
            payload = _BRACKETED_PARAM.sub(lambda m: f"%5B{m.group(1)}%5D", payload)
            # A short block is a fragment: ``GET /api/jolokia/list HTTP/1.1``
            # with no body and no target teaches only that an endpoint exists,
            # which is what the Worker already guesses.  Set the floor at 60
            # chars, the same threshold the solved-Run extractor uses, so the
            # two sources agree on what counts as a payload.
            #
            # Script invocations and bare paths legitimately collapse below that
            # once the host is parameterised -- ``python3 poc.py {{target_url}}``
            # is 33 chars and is the entire nginx CVE-2017-7529 recipe.  Judging
            # them by the same floor would drop the payload for being honest.
            floor = _SCRIPT_MIN_CHARS if form in ("script", "path") else 60
            if len(payload.strip()) < floor:
                stats.too_short += 1
                continue
            key = (cve, payload[:200])
            if key in seen:
                continue
            seen.add(key)

            technique = _technique(title, payload)
            # Basic auth is shipped as ``admin:admin`` in vulhub's compose file
            # and is parameterised here, so say so where the Worker will read it.
            needs_basic_auth = "{{basic_auth}}" in payload or "{{username}}" in payload
            preconditions = [
                f"目标产品为 {product}，且版本落在 {cve} 的影响范围内（先确认版本，再发载荷）",
                "README 中的服务端口需按实际目标替换",
            ]
            if needs_basic_auth:
                preconditions.append(
                    "载荷含 Basic 认证：vulhub 默认 admin:admin，本平台靶机常改，先试默认再爆破"
                )
            entries.append(
                {
                    "id": f"vulhub:{product.casefold()}:{cve.casefold()}"
                          + (f":{index}" if index > 1 else ""),
                    "kind": "poc",
                    "title": f"{title} ({cve})" if cve not in title.upper() else title,
                    "technique": technique,
                    "summary": f"{product} / {cve}。载荷取自上流 README 的复现段落，已按实例值参数化。",
                    "preconditions": preconditions,
                    "target_stack": [product, cve],
                    "payload_template": payload[:4000],
                    "verification_method": [
                        "按 payload 原样发送；命中判据是响应出现该 CVE 的预期特征（回显、报错、状态码或读到的文件内容）。",
                        "第一次没有预期响应时换下一条载荷，不要在同一句上反复微调。",
                    ],
                    "known_failures": [
                        "机制导入，从未在本平台靶机上验证过；版本不符时不会生效。",
                    ],
                    "steps": [
                        f"确认 {product} 版本在 {cve} 影响范围内",
                        "把 payload 中的 {{target_url}} / {{host}} 换成实际目标",
                        "发送并把响应与预期特征比对",
                    ],
                    "post_exploitation": [],
                    "provenance": {
                        "source_type": "reference-corpus",
                        "source_ref": "vulhub",
                        # ``KnowledgeProvenance`` forbids extra keys, so the
                        # README path rides in ``notes`` rather than a new field
                        # (which would fail schema validation for all 259 rows).
                        "source_url": f"https://github.com/vulhub/vulhub/tree/master/{product}/{cve_dir.name}",
                        "notes": f"提取自 {str(readme.relative_to(source)).replace(chr(92), '/')} 的复现段落",
                    },
                    "trust_level": "unverified",
                    "verified_solve": False,
                    "published_at": None,
                }
            )
    stats.emitted = len(entries)
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(DEFAULT_SOURCE))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--show", type=int, default=0)
    args = parser.parse_args(argv)

    source = Path(args.source)
    if not source.is_dir():
        print(f"源目录不存在：{source}", file=sys.stderr)
        return 2

    stats = Stats()
    entries = build(source, stats)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + ("\n" if entries else ""),
                   encoding="utf-8")

    print(
        f"扫描 {stats.cves} 个 CVE 目录（{stats.readmes} 个有 README）→ 提取 {len(entries)} 条载荷\n"
        f"  跳过：无复现段落 {stats.no_repro_section}；段落里无可执行块 {stats.no_runnable_block}；"
        f"非执行块（docker/npm/git）{stats.setup_only}；过短片段 {stats.too_short}"
    )
    counts = Counter(e["technique"] for e in entries)
    print("  技法分布：" + "，".join(f"{k} {v}" for k, v in counts.most_common()))
    print(f"\n写入 {out}（trust_level=unverified，不参与 FIRST ACTION）")

    if args.show:
        print(f"\n=== 前 {min(args.show, len(entries))} 条 ===")
        for entry in entries[: args.show]:
            print(f"\n[{entry['technique']}] {entry['title'][:70]}")
            print(f"  id: {entry['id']}")
            print("  payload:")
            for line in entry["payload_template"].splitlines()[:12]:
                print(f"    {line[:150]}")
    return 0 if entries else 1


if __name__ == "__main__":
    sys.exit(main())
