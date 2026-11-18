"""Convert the Des-CTF-Knowledge reference corpus into curated JSONL items.

Why a converter rather than copying the markdown: the retrieval layer ranks
``KnowledgeItem`` documents with BM25 over title/summary/steps, so one 200 KB
article is a single document that matches nothing in particular and loses to a
short, focused entry every time.  The unit a Worker's intent can actually match
is a *section*: one technique, one payload shape, one failure mode.

The output is deliberately ``trust_level="unverified"``.  These are external
reference notes -- no human reviewed them against a live target in this
project, and the corpus audit already distinguishes them from this project's
own distilled templates (``provenance.source_type="learned"``).

Usage::

    python scripts/import_des_ctf_knowledge.py --dry-run
    python scripts/import_des_ctf_knowledge.py --out data/knowledge/des-ctf.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SOURCE = Path("D:/desktop/毕业设计/参考项目/Des-CTF-Knowledge")
DEFAULT_OUTPUT = Path("data/knowledge/des-ctf.jsonl")

#: Files that carry technique knowledge, mapped to the ``technique`` label the
#: retriever ranks on.  Anything not listed here is skipped: the repo also
#: holds licence, changelog, progress logs and AI-facing indexes, none of which
#: is a technique a Worker can execute.
TECHNIQUE_BY_FILE: dict[str, str] = {
    "SQL.md": "SQL injection",
    "命令执行.md": "Command injection and RCE",
    "文件上传漏洞.md": "File upload bypass",
    "文件包含.md": "Local file inclusion and PHP wrappers",
    "SSRF漏洞.md": "Server-side request forgery",
    "PHP反序列化漏洞总结.md": "PHP deserialization",
    "SSTI.md": "Server-side template injection",
    "php代码审计.md": "PHP source review",
    "JWT.md": "JWT forgery",
    "压缩包总结.md": "Archive cracking",
    "图片隐写.md": "Image steganography",
    "音频隐写.md": "Audio steganography",
    "PAYLOAD-CHEATSHEET.md": "Payload cheatsheet",
}

#: Directories whose markdown files are techniques rather than indexes.
#:
#: ``CTF常用脚本及工具`` is deliberately absent: it holds runnable scripts plus
#: language-primer notes ("python-note", basic slicing/encoding syntax).  Those
#: notes have no technique to retrieve -- converted, they became 49 items whose
#: "steps" were statements like ``- 切片`` -- and they crowded real techniques
#: out of the top-k window.
TECHNIQUE_BY_DIR: dict[str, str] = {
    "WP汇总": "CTF writeup technique",
    "工具使用": "Tool usage",
}

#: Files that are notes about a language, not about a target.
_PRIMER_FILES = re.compile(r"(python-note|notes?\.md$|基础语法|语法笔记)", re.IGNORECASE)

#: Section headings that are navigation, not technique.
_SKIP_HEADINGS = re.compile(
    r"^(目录|索引|参考资料|相关资源|参考|更新记录|更新日志|免责声明|声明|前言|简介|"
    r"table of contents|contents|references?|changelog)$",
    re.IGNORECASE,
)

_HEADING_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_LIST_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.、)]\s+)(\S.*)$")
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？；.!?])\s*")
_VERIFICATION_HINTS = (
    "验证",
    "确认",
    "判断",
    "回显",
    "成功",
    "证明",
    "说明",
    "观察",
    "verify",
    "confirm",
    "check whether",
)

_MAX_SUMMARY = 420
_MAX_PAYLOAD = 1500
_MAX_STEPS = 6
_MAX_ITEM_TITLE = 120
_MIN_INFORMATIVE_CHARS = 60


@dataclass
class Section:
    """One heading and everything under it, code fences included."""

    level: int
    title: str
    lines: list[str] = field(default_factory=list)


@dataclass
class Skipped:
    reason: str
    count: int = 0


def split_sections(text: str) -> tuple[str, list[Section]]:
    """Return the document title and its ``##`` / ``###`` sections.

    Fence-aware on purpose: SQL.md contains lines beginning with ``#`` inside
    fenced SQL comments, and a naive heading scan cuts the document in the
    middle of a payload.
    """

    doc_title = ""
    sections: list[Section] = []
    current: Section | None = None
    in_fence = False

    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            if current is not None:
                current.lines.append(line)
            continue
        if not in_fence:
            if line.startswith("# ") and not doc_title:
                doc_title = line[2:].strip()
                continue
            match = _HEADING_RE.match(line)
            if match:
                current = Section(level=len(match.group(1)), title=match.group(2).strip())
                sections.append(current)
                continue
        if current is not None:
            current.lines.append(line)
    return doc_title, sections


def extract_code(lines: list[str]) -> str:
    """First fenced block, which is the payload a reader would copy."""

    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in lines:
        if _FENCE_RE.match(line):
            if current is None:
                current = []
                blocks.append(current)
            else:
                current = None
            continue
        if current is not None:
            current.append(line)
    for block in blocks:
        text = "\n".join(block).strip()
        if text:
            return text[:_MAX_PAYLOAD]
    return ""


def extract_plain(lines: list[str]) -> list[str]:
    """Lines outside fences, which is prose rather than payload."""

    plain: list[str] = []
    in_fence = False
    for line in lines:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        stripped = line.strip()
        if stripped:
            plain.append(stripped)
    return plain


def extract_steps(plain: list[str]) -> list[str]:
    """Bullet or numbered items, falling back to the first prose sentences."""

    items = [match.group(1).strip() for match in map(_LIST_RE.match, plain) if match]
    if items:
        return [item[:280] for item in items[:_MAX_STEPS]]
    sentences = [
        sentence.strip()[:280]
        for sentence in _SENTENCE_SPLIT.split(" ".join(plain))
        if len(sentence.strip()) >= 20
    ]
    return sentences[:_MAX_STEPS]


def extract_verification(plain: list[str]) -> list[str]:
    """Sentences that state how the technique is confirmed against a target."""

    joined = " ".join(plain)
    candidates = [
        sentence.strip()[:280]
        for sentence in _SENTENCE_SPLIT.split(joined)
        if any(hint in sentence.casefold() for hint in _VERIFICATION_HINTS)
        and len(sentence.strip()) >= 15
    ]
    if candidates:
        return list(dict.fromkeys(candidates))[:2]
    return [
        "Send the same request twice with a control value and the probe value, "
        "and compare the two responses before concluding anything."
    ]


def technique_for(path: Path) -> str:
    if path.name in TECHNIQUE_BY_FILE:
        return TECHNIQUE_BY_FILE[path.name]
    for part in path.parts:
        if part in TECHNIQUE_BY_DIR:
            return TECHNIQUE_BY_DIR[part]
    return ""


def item_id(relative: str, heading: str) -> str:
    digest = hashlib.sha1(f"{relative}#{heading}".encode("utf-8")).hexdigest()[:10]
    return f"des-ctf-{digest}"


def build_item(
    *,
    path: Path,
    relative: str,
    technique: str,
    section: Section,
) -> dict[str, object] | None:
    plain = extract_plain(section.lines)
    payload = extract_code(section.lines)
    prose = " ".join(plain)
    if len(prose) < _MIN_INFORMATIVE_CHARS and not payload:
        return None
    steps = extract_steps(plain)
    if not steps:
        return None
    summary = prose[:_MAX_SUMMARY].strip()
    if not summary:
        summary = f"{section.title} ({payload.splitlines()[0][:80]})"
    return {
        "id": item_id(relative, section.title),
        "kind": "poc" if payload else "technique",
        "title": f"{path.stem} — {section.title}"[:_MAX_ITEM_TITLE],
        "technique": technique,
        "summary": summary,
        "preconditions": [],
        "target_stack": [],
        "payload_template": payload or None,
        "verification_method": extract_verification(plain),
        "known_failures": [],
        "steps": steps,
        # ``case``/``negative`` require known_failures, and a converted section
        # cannot honestly assert one -- it would be an invented claim about a
        # route nobody in this project ran.  ``technique``/``poc`` are the
        # truthful kinds here.
        "provenance": {
            "source_type": "reference-corpus",
            "source_ref": f"Des-CTF-Knowledge/{relative}#{section.title}",
            "source_url": None,
            "notes": "Converted from an external CTF knowledge collection; not verified against a live target.",
        },
        "published_at": None,
        "trust_level": "unverified",
    }


def collect(source: Path, *, include_articles: bool) -> tuple[list[dict[str, object]], dict[str, int]]:
    items: list[dict[str, object]] = []
    skipped: dict[str, int] = {}
    seen_ids: set[str] = set()

    for path in sorted(source.rglob("*.md")):
        relative = path.relative_to(source).as_posix()
        if ".git" in path.parts:
            continue
        if relative.endswith(".idx.md"):
            # Machine-facing line-range indexes: they describe where to read,
            # not what to do.
            skipped["index_file"] = skipped.get("index_file", 0) + 1
            continue
        if "articles" in path.parts and not include_articles:
            skipped["articles_excluded"] = skipped.get("articles_excluded", 0) + 1
            continue
        technique = technique_for(path)
        if _PRIMER_FILES.search(path.name):
            skipped["language_primer"] = skipped.get("language_primer", 0) + 1
            continue
        if not technique:
            skipped["not_a_technique_file"] = skipped.get("not_a_technique_file", 0) + 1
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            skipped["unreadable"] = skipped.get("unreadable", 0) + 1
            continue
        _doc_title, sections = split_sections(text)
        for section in sections:
            if _SKIP_HEADINGS.match(section.title.strip(" :：")):
                skipped["navigation_heading"] = skipped.get("navigation_heading", 0) + 1
                continue
            item = build_item(
                path=path, relative=relative, technique=technique, section=section
            )
            if item is None:
                skipped["too_little_content"] = skipped.get("too_little_content", 0) + 1
                continue
            if str(item["id"]) in seen_ids:
                skipped["duplicate_id"] = skipped.get("duplicate_id", 0) + 1
                continue
            seen_ids.add(str(item["id"]))
            items.append(item)
    return items, skipped


def write_jsonl(items: list[dict[str, object]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--include-articles", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if not args.source.exists():
        print(f"source not found: {args.source}", file=sys.stderr)
        return 2

    items, skipped = collect(args.source, include_articles=args.include_articles)
    kinds: dict[str, int] = {}
    by_technique: dict[str, int] = {}
    for item in items:
        kinds[str(item["kind"])] = kinds.get(str(item["kind"]), 0) + 1
        label = str(item["technique"])
        by_technique[label] = by_technique.get(label, 0) + 1

    print(f"items: {len(items)}")
    print(f"kinds: {json.dumps(kinds, ensure_ascii=False)}")
    print(f"techniques: {json.dumps(by_technique, ensure_ascii=False, indent=2)}")
    print(f"skipped: {json.dumps(skipped, ensure_ascii=False)}")
    if args.dry_run:
        for sample in items[:3]:
            print(json.dumps(sample, ensure_ascii=False)[:400])
        return 0

    write_jsonl(items, args.out)
    print(f"written: {args.out}")

    # Validate through the same loader the runtime uses, so a malformed entry
    # fails here instead of on the first Run that tries to retrieve it.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
    from app.knowledge.loader import load_knowledge_items

    loaded = load_knowledge_items(args.out)
    print(f"loader accepted: {len(loaded)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
