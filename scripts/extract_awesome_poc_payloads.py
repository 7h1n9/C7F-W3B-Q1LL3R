"""Extract payloads from the vendored Awesome-POC corpus.

Why a second extractor instead of extending the first: the *layout* differs, and
the difference is exactly what the first one assumed.

| | vulhub | Awesome-POC |
|---|---|---|
| shape | ``product/CVE-xxxx-yyyy/README.md`` | ``category/<title> CVE-xxxx-yyyy.md`` |
| one CVE spans | 2 files (en + zh) | 1 file, but titled in prose |
| heading style | ``## 漏洞复现`` / ``## 漏洞详情`` | ``## 漏洞复现`` / ``## 漏洞POC`` / ``### 反弹 shell`` |
| fenced blocks | 0-4 | **up to 16** |

So: the CVE id comes from the *filename*, not a parent directory; there is no
language duplicate to dedupe; and the "reproduce" section frequently has
siblings (``## 漏洞 POC``, ``## 利用``) that all carry runnable requests.

This corpus exists to fill a measured gap.  After the vulhub import, 9 of the
91 CVE-titled arena boxes still reached no recipe, and 7 of those 9 have a page
here -- Tomcat Ghostcat's vulhub README only links to external tooling, while
this corpus carries its AJP request inline.

Reuses the vulhub extractor's section/fence/placeholder machinery rather than
copying it: two extractors that disagree on what counts as a payload is how a
corpus acquires entries nobody can reproduce.
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
sys.path.insert(0, str(ROOT / "scripts"))

# The section/fence/placeholder/technique logic is shared on purpose.
from extract_vulhub_payloads import (  # noqa: E402
    _fenced_blocks,
    _is_runnable,
    _parameterise,
    _read,
    _repro_sections,
    _technique,
    _INLINE_PATH,
    _INLINE_REQUEST,
    _INLINE_SCRIPT,
    _INLINE_ATTACK,
    _PROBE_ONLY,
    _SCRIPT_MIN_CHARS,
)

DEFAULT_SOURCE = ROOT / "data" / "sources" / "awesome-poc"
DEFAULT_OUT = ROOT / "data" / "knowledge" / "candidates" / "awesome-poc-payloads.jsonl"

#: This corpus documents a lot of exploitation through *interactive tool
#: sessions* rather than raw requests -- ``smbclient -L //10.8.0.1/ -N``,
#: ``msf6 > use exploit/linux/samba/is_known_pipename``,
#: ``nmap --script smb-vuln-*``.  The vulhub corpus is almost all HTTP, so its
#: ``_RUNNABLE`` does not know these, and Samba CVE-2017-7494 yielded 0 payloads
#: from 14 code blocks because of it.
_INTERACTIVE = re.compile(
    r"^\s*(?:"
    r"smbclient|smbmap|enum4linux|rpcclient|crackmapexec|net\s+use|"
    r"m?s[fh]\d*\s*>?\s*(?:use|set|run|exploit|search)\b|"
    r"nmap\b|nc\b|ncat\b|socat\b|telnet\b|"
    r"redis-cli|mysql\s+-|psql\s+|mongo\b|sqlmap\b|"
    r"impacket-\w+|python3?\s+-m\s+pwn|"
    r"\$?\s*(?:nc|bash|sh|python3?)\s+-[a-z]*e\b"
    r")",
    re.I,
)

#: A hard-coded target address in this corpus is always the author's own range
#: (``10.8.0.1``, ``192.168.1.1``).  Left literal it points the Worker at
#: somebody else's host, so it is parameterised like any other placeholder.
_TARGET_IP = re.compile(r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b")

#: Blocks that are a *payload for a browser*, not a request a Worker can send.
#: This corpus documents CSRF with a whole HTML page (74cms CVE-2019-11374) and
#: XSS with a bare ``<img src=...>``.  A Worker cannot "send" either without a
#: victim's browser, so shipping them as recipes teaches a step it can never
#: complete.  Filtered at extraction, where the intent is still visible.
_NOT_AN_ATTACK = re.compile(r"^\s*(?:<!DOCTYPE|<html|<img\s|<script|<svg|<iframe)", re.I)

_CVE = re.compile(r"(CVE-\d{4}-\d{4,})", re.I)

#: ``Some references:`` and other trailing sections list links, not payloads.
_REFERENCE_SECTION = re.compile(
    r"^#{1,6}\s*(?:references?|参考(?:链接|资料)?|相关链接|see also)\s*$", re.I
)

#: Sections that carry runnable requests in this corpus but are not called
#: "reproduce".  ``## 漏洞 POC`` is the most common: it holds a bare script.
_EXTRA_SECTIONS = re.compile(
    r"^#{1,4}\s*(?:"
    r"漏洞\s*poc|poc\s*(?:代码|脚本)?|利用脚本|利用代码|exp|verify|检测"
    r")\s*$",
    re.I,
)


@dataclass
class Stats:
    files: int = 0
    with_cve: int = 0
    no_section: int = 0
    no_block: int = 0
    too_short: int = 0
    not_attack: int = 0
    emitted: int = 0
    inline: int = 0


def _heading_level(line: str, *, in_fence: bool = False) -> int:
    """Heading depth, or 0 inside a code fence.

    A fence containing ``# apt install smbclient`` is a shell comment, but read
    naively it is an H1 that closes the section it sits in. That is exactly what
    made Samba CVE-2017-7494 yield 0 payloads from 14 code blocks: its
    ``## 漏洞复现`` section ended two lines in, at a comment.

    The robust test is structural rather than lexical -- track fences and return
    0 for every line inside one -- so a comment mentioning ``curl`` or one
    mentioning nothing at all are handled the same way.
    """

    if in_fence:
        return 0
    match = re.match(r"^(#{1,6})\s+\S", line)
    return len(match.group(1)) if match else 0


def _usable_sections(markdown: str) -> list[str]:
    """Reproduce/POC sections, excluding link-only reference blocks."""

    lines = markdown.splitlines()
    # Line indices that sit inside a code fence, so heading detection can skip
    # them (see ``_heading_level``).
    fenced: set[int] = set()
    open_fence = False
    for index, line in enumerate(lines):
        if re.match(r"^\s*```", line):
            fenced.add(index)
            open_fence = not open_fence
            continue
        if open_fence:
            fenced.add(index)

    def level_at(index: int) -> int:
        return _heading_level(lines[index], in_fence=index in fenced)

    # Boundaries of any "references" section, so its content is dropped even
    # when it sits inside a parent exploit section.
    drop: list[tuple[int, int]] = []
    index = 0
    while index < len(lines):
        if index not in fenced and _REFERENCE_SECTION.match(lines[index].strip()):
            level = level_at(index)
            end = index + 1
            while end < len(lines):
                if end in fenced:
                    end += 1
                    continue
                next_level = level_at(end)
                if next_level and next_level <= level:
                    break
                end += 1
            drop.append((index, end))
            index = end
        else:
            index += 1

    kept = list(lines)
    dropped_text = {id(lines[i]) for start, end in drop for i in range(start, end)}
    kept = [line for line in lines if id(line) not in dropped_text]

    sections = _repro_sections("\n".join(kept))
    for extra in _extra_sections(kept):
        if extra not in sections:
            sections.append(extra)
    return sections


def _extra_sections(lines: list[str]) -> list[str]:
    sections: list[str] = []
    current: list[str] | None = None
    level = 0
    open_fence = False
    for line in lines:
        if re.match(r"^\s*```", line):
            open_fence = not open_fence
            if current is not None:
                current.append(line)
            continue
        this = _heading_level(line, in_fence=open_fence)
        if this:
            if current is not None and this <= level:
                sections.append("\n".join(current))
                current = None
            if current is None and _EXTRA_SECTIONS.match(line.strip()):
                current = []
                level = this
                continue
        if current is not None:
            current.append(line)
    if current is not None:
        sections.append("\n".join(current))
    return [s for s in sections if s.strip()]


def _title_of(markdown: str, fallback: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def build(source: Path, stats: Stats | None = None) -> list[dict]:
    stats = stats or Stats()
    entries: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for path in sorted(source.rglob("*.md")):
        stats.files += 1
        match = _CVE.search(path.stem)
        if not match:
            continue
        # The category is the top-level folder this page sits under, which is
        # as close to a "product" as this corpus gets.
        rel = path.relative_to(source)
        category = rel.parts[0] if len(rel.parts) > 1 else "misc"
        cve = match.group(1).upper()
        stats.with_cve += 1

        markdown = _read(path)
        if not markdown:
            continue

        sections = _usable_sections(markdown)
        if not sections:
            stats.no_section += 1
            continue

        blocks: list[tuple[str, str]] = []
        for section in sections:
            for _language, body in _fenced_blocks(section):
                if not _is_runnable(body) and not _INTERACTIVE.match(body.strip()):
                    continue
                if _NOT_AN_ATTACK.search(body):
                    stats.not_attack += 1
                    continue
                blocks.append(("fence", body))
            for found in _INLINE_REQUEST.finditer(section):
                candidate = found.group(1).strip()
                # Same probe filter as the vulhub extractor: an inline URL
                # bypasses ``_is_runnable``, so ``?username=<script>alert(1)
                # </script>`` would otherwise be filed as an exploit.
                if _INLINE_ATTACK.search(candidate) and not _PROBE_ONLY.search(candidate):
                    blocks.append((
                        "fence",
                        f"GET {candidate} HTTP/1.1\nHost: {{{{host}}}}\nConnection: close\n",
                    ))
                    stats.inline += 1
            for found in _INLINE_SCRIPT.finditer(section):
                candidate = found.group(1).strip()
                if re.search(r"\d|your-ip|target|127\.0\.0\.1|localhost", candidate, re.I):
                    blocks.append(("script", candidate))
                    stats.inline += 1
            for found in _INLINE_PATH.finditer(section):
                candidate = found.group(1).strip()
                blocks.append((
                    "fence",
                    f"GET {candidate} HTTP/1.1\nHost: {{{{host}}}}\nConnection: close\n",
                ))
                stats.inline += 1

        if not blocks:
            stats.no_block += 1
            continue

        title = _title_of(markdown, path.stem)
        for index, (form, body) in enumerate(blocks, 1):
            payload, _placeholders = _parameterise(body)
            # The author's own lab address, replaced with the placeholder every
            # other entry uses so the payload cannot be sent at a stranger.
            payload = _TARGET_IP.sub("{{target_url}}", payload)
            floor = _SCRIPT_MIN_CHARS if form in ("script", "path") else 60
            if len(payload.strip()) < floor:
                stats.too_short += 1
                continue
            key = (cve, payload[:200])
            if key in seen:
                continue
            seen.add(key)

            entries.append(
                {
                    "id": f"awesome-poc:{cve.casefold()}" + (f":{index}" if index > 1 else ""),
                    "kind": "poc",
                    "title": title if cve in title.upper() else f"{title} ({cve})",
                    "technique": _technique(title, payload),
                    "summary": f"{category} / {cve}。载荷取自 Awesome-POC 的复现段落，已按实例值参数化。",
                    "preconditions": [
                        f"目标产品与版本需落在 {cve} 的影响范围内（先确认版本，再发载荷）",
                        "载荷中的 {{host}} / {{target_url}} 按实际目标替换",
                    ],
                    "target_stack": [category, cve],
                    "payload_template": payload[:4000],
                    "verification_method": [
                        "按 payload 原样发送；命中判据是响应出现该 CVE 的预期特征（回显、报错或读到的文件内容）。",
                        "第一次没有预期响应时换下一条载荷，不要在同一句上反复微调。",
                    ],
                    "known_failures": [
                        "机制导入，从未在本平台靶机上验证过；版本不符时不会生效。",
                    ],
                    "steps": [
                        f"确认目标版本在 {cve} 影响范围内",
                        "把 payload 中的占位符换成实际目标",
                        "发送并把响应与预期特征比对",
                    ],
                    "post_exploitation": [],
                    "provenance": {
                        "source_type": "reference-corpus",
                        "source_ref": "awesome-poc",
                        "source_url": "https://github.com/Threekiii/Awesome-POC",
                        "notes": f"提取自 {rel.as_posix()} 的复现段落",
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
    out.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + ("\n" if entries else ""),
        encoding="utf-8",
    )

    print(
        f"扫描 {stats.files} 个 md（{stats.with_cve} 个带 CVE 号）→ 提取 {len(entries)} 条载荷\n"
        f"  跳过：无复现段落 {stats.no_section}；段落里无可执行块 {stats.no_block}；"
        f"过短 {stats.too_short}；浏览器侧载荷 {stats.not_attack}\n"
        f"  行内代码补捞：{stats.inline}"
    )
    counts = Counter(e["technique"] for e in entries)
    print("  技法分布：" + "，".join(f"{k} {v}" for k, v in counts.most_common()))
    print(f"\n写入 {out}")

    if args.show:
        for entry in entries[: args.show]:
            print(f"\n[{entry['technique']}] {entry['title'][:70]}")
            for line in entry["payload_template"].splitlines()[:8]:
                print(f"    {line[:140]}")
    return 0 if entries else 1


if __name__ == "__main__":
    sys.exit(main())
