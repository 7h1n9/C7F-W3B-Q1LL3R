"""Audit the extracted payload candidates before anything is promoted.

The extractor only found *where* the decisive command was; this decides whether
the entry is usable at all.  Three rounds of hand-checking the first batch found
that most of the obvious metrics lie, so each rule here is written against a
concrete failure observed in the real file:

* **A flag hides in base64.** ``[ACTF2020]Include`` carries
  ``PD9waHAK...Q1RGMns...`` -- ``echo '<base64>' | base64 -d`` -- and the plaintext
  scan called the file clean.  Any leak check MUST decode first.
* **A short template can still be a truncated command.** The collector clipped
  commands at 300 chars *upstream* (``cli_driver.py:1137``), and the scrubber
  then shortened them further, so ``len(template) >= 290`` misses entries whose
  raw command was exactly 300.  The raw length has to come from the database.
* **"Decisive" is not the same as "exploitation".** For a command-injection box
  the winning step is ``curl shell.php?cmd=cat /flag`` -- that presumes a shell
  already planted, and learning it teaches nothing replayable.
* **A payload that only creates a file sends nothing.** ``cat > f2.jpg <<EOF``
  ends the trace; the upload is a later step.

Verdicts:
  ``accept``  complete, self-contained, and genuinely the exploitation step.
  ``repair``  right idea, missing prerequisites -- keep, but do not promote.
  ``reject``  not an exploitation step, unsafe, or unreplayable as recorded.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import sys
import urllib.parse
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = ROOT / "data" / "knowledge" / "candidates" / "payload-candidates.jsonl"

#: Any real flag, in whatever encoding the trace happened to carry it.
_FLAG = re.compile(r"(?:CTF2|flag|FLAG|DASCTF|ctf)\{[^}]{6,}\}")

#: The collector's upstream clip.  A raw command of exactly this length is
#: truncated even when the text looks like it ended cleanly.
_UPSTREAM_CLIP = 300

#: A command aimed at a shell that a *previous* step planted.
_RETRIEVAL = re.compile(
    r"(?:shell\.php|/upload/[a-z0-9_.]+\.phtml|twentyfifteen/404\.php|"
    r"/api/s\.jsp|cmd=(?:cat|env|grep|id)\b|\?c=(?:cat|id|env)\b)",
    re.I,
)

#: Commands that read artifacts a previous step produced.
_DEPENDENCIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("前序文件", re.compile(r"scratch/|/tmp/\{\{scratch\}\}|cat tokens|grep TOKB|cat jsp|cat valid|(?:^|\s)s2\.py|read\.sh|sim\.py")),
    ("会话", re.compile(r"-b cj|-c cj")),
    ("前序变量", re.compile(r"\$A\b|\$TOK\b|\$B64\b|\$SIGN\b")),
    ("已植入 webshell", re.compile(r"phtml_t\d|/upload/[a-z0-9]+\.phtml")),
    # A helper the Worker wrote in an *earlier* step and then invoked.  These
    # look like self-contained one-liners but are just the trigger; ``./q.sh``
    # and ``import q`` carry the actual payload, and neither exists in the
    # workspace any more, so the entry teaches nothing.
    ("前序脚本", re.compile(r"\./q\.sh|\./g\.sh|\./run\.sh|(?:^|\s)q\.q\(|import q\b")),
    # A payload that names a challenge-specific path learned by reading the
    # target's source is not transferable; it is a writeup, not a technique.
    ("题解专用路径", re.compile(r"ffffllllaaaagggg|/usr/src/static|suctf%E3%80%82|/flag\*|cat /flag\b")),
)

#: A heredoc that opens a script but never closes it: the recorded command is
#: cut mid-body even when the raw length is well under the collector's limit
#: (``[CISCN]Web4`` ends at ``class RCE:``).  Checked by counting openers and
#: terminators, which catches an unbalanced block regardless of length.
_HEREDOC_OPEN = re.compile(r"<<\s*'?([A-Za-z_][A-Za-z0-9_]*)'?")

#: ``cat > x <<EOF`` with no send in the same command.
_CREATE_ONLY = re.compile(r"cat > \S+ <<")
_SENDS = re.compile(r"curl|wget|requests\.(?:post|get)|python3? \S+\.(?:py|sh)|nc \S+ \d+|send\(|\.submit\(")


def _decoded_views(text: str) -> list[str]:
    """``text`` plus every plausible decoding, so a leak cannot hide.

    Base64 is tried first because that is how the flag actually got in (a
    ``base64 -d`` in the payload itself).  URL and hex follow: both are common
    and both are cheap.
    """

    views = [text]
    for match in re.finditer(r"[A-Za-z0-9+/]{40,}={0,2}", text):
        blob = match.group(0)
        try:
            views.append(base64.b64decode(blob + "=" * (-len(blob) % 4)).decode("utf-8", "replace"))
        except (binascii.Error, ValueError):
            pass
    if "%7b" in text.casefold():
        views.append(urllib.parse.unquote(text))
    for match in re.finditer(r"(?:[0-9a-fA-F]{2}){30,}", text):
        try:
            views.append(binascii.unhexlify(match.group(0)).decode("utf-8", "replace"))
        except (binascii.Error, ValueError):
            pass
    return views


def find_flag(entry: dict) -> str | None:
    """Return the first flag found in any encoding, or ``None``."""

    blob = json.dumps(entry, ensure_ascii=False)
    for view in _decoded_views(blob):
        hit = _FLAG.search(view)
        if hit:
            return hit.group(0)
    return None


def raw_command_lengths(run_ids: list[str]) -> dict[str, dict[int, int]]:
    """``{run_id: {sequence: len(raw command)}}`` straight from the event table.

    Needed because the stored template may be shorter than the original after
    redaction and scrubbing, which makes a naive length check miss a truncated
    command entirely.
    """

    try:
        import pymysql
    except ImportError:  # pragma: no cover - the audit must still run
        return {}

    import os

    try:
        conn = pymysql.connect(
            host=os.getenv("APP_AUDIT_DB_HOST", "127.0.0.1"),
            port=int(os.getenv("APP_AUDIT_DB_PORT", "3307")),
            user=os.getenv("APP_AUDIT_DB_USER", "ctf_agent"),
            password=os.getenv("APP_AUDIT_DB_PASSWORD", "ctf_agent"),
            database=os.getenv("APP_AUDIT_DB_NAME", "ctf_agent"),
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
            connect_timeout=8,
        )
    except Exception:
        return {}

    out: dict[str, dict[int, int]] = {}
    try:
        with conn.cursor() as cur:
            for run_id in set(run_ids):
                cur.execute(
                    """SELECT sequence, LENGTH(JSON_UNQUOTE(JSON_EXTRACT(payload_json,
                                                          '$.payload.command'))) AS n
                       FROM run_events
                       WHERE run_id = %s AND event_type = 'muteki.worker_command'
                         AND JSON_EXTRACT(payload_json, '$.payload.kind') = 'tool'""",
                    (run_id,),
                )
                out[run_id] = {r["sequence"]: int(r["n"] or 0) for r in cur.fetchall()}
    finally:
        conn.close()
    return out


def _unclosed_heredoc(template: str) -> str | None:
    """The heredoc terminator named in ``template`` that never appears again.

    A truncated script body is unmistakable once checked structurally: the
    opener is recorded, the body is not finished, so the terminator is missing.
    Length alone cannot see this -- ``[CISCN]Web4`` is only 257 chars.
    """

    for match in _HEREDOC_OPEN.finditer(template):
        terminator = match.group(1)
        body = template[match.end():]
        if not re.search(rf"(?m)^\s*{re.escape(terminator)}\s*$", body):
            return terminator
    return None


def judge(entry: dict, raw_len: int | None) -> tuple[str, list[str]]:
    """``(verdict, reasons)`` for one candidate."""

    reasons: list[str] = []
    template = entry.get("payload_template") or ""

    if find_flag(entry):
        return "reject", ["含真实 flag（含 base64 等编码形态）"]

    # Truncation: prefer the raw length, fall back to a conservative text test.
    length = raw_len if raw_len else len(template)
    if length >= _UPSTREAM_CLIP:
        return "reject", [f"采集端截断（原始命令 {length} 字符，上游上限 {_UPSTREAM_CLIP}）"]

    unclosed = _unclosed_heredoc(template)
    if unclosed:
        return "reject", [f"heredoc 未闭合（缺 {unclosed} 终止符），脚本体被切掉"]

    if _RETRIEVAL.search(template):
        return "reject", ["植入成功后的回读，不是利用步骤"]

    if _CREATE_ONLY.search(template) and not _SENDS.search(template):
        return "repair", ["只创建了载荷文件，未发送"]

    # A credential the collector masked is gone for good: ``[redacted]`` in a
    # DSN cannot be filled in, so the entry is a demonstration, not a payload.
    if "[redacted]" in template:
        return "repair", ["凭证在采集时被脱敏为 [redacted]，无法填写"]

    for label, pattern in _DEPENDENCIES:
        if pattern.search(template):
            reasons.append(f"依赖{label}")
    if reasons:
        return "repair", reasons

    # A payload whose only action *is* the flag reveal has nothing left to
    # teach once the placeholders are filled: ``psql ... -c "COPY ... FROM
    # PROGRAM 'env'"`` is a real technique; ``for c in "cat /flag"...`` is not.
    if not re.search(r"curl|wget|psql|python3?|java|nc |ssh|POST|gremlin", template, re.I):
        return "repair", ["不含向目标发请求的动作"]

    return "accept", []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(DEFAULT_SOURCE))
    parser.add_argument("--json-out", default=str(ROOT / "data" / "knowledge" / "candidates" / "review-verdict.json"))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    entries = [json.loads(line) for line in Path(args.source).read_text(encoding="utf-8").splitlines() if line.strip()]
    raw = raw_command_lengths([e["provenance"]["run_id"] for e in entries])

    results = []
    for index, entry in enumerate(entries, 1):
        prov = entry["provenance"]
        raw_len = raw.get(prov["run_id"], {}).get(prov["decisive_sequence"])
        verdict, reasons = judge(entry, raw_len)
        results.append(
            {
                "index": index,
                "verdict": verdict,
                "reasons": reasons,
                "technique": entry.get("technique"),
                "title": entry.get("title"),
                "run_id": prov["run_id"],
                "decisive_sequence": prov["decisive_sequence"],
                "raw_length": raw_len,
            }
        )

    counts = Counter(r["verdict"] for r in results)
    Path(args.json_out).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    if not args.quiet:
        for verdict in ("reject", "repair", "accept"):
            subset = [r for r in results if r["verdict"] == verdict]
            print(f"\n=== {verdict.upper()} ({len(subset)}) ===")
            for item in subset:
                note = "; ".join(item["reasons"]) or "完整 / 独立 / 是真实利用步骤"
                print(f"  [{item['index']:02d}] {str(item['technique'])[:18]:18s} {note}")
                print(f"        {str(item['title'])[:70]}")

    print(
        f"\n共 {len(results)} 条：accept {counts['accept']} / "
        f"repair {counts['repair']} / reject {counts['reject']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
