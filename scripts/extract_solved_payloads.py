"""Extract verified payloads from Runs that actually captured a flag.

Why from solved Runs rather than the internet.  Measured 2026-11-05
(``docs/notes/2026-11/2026-11-05-failure-attribution.md``): unsolved Runs are not crashing
(243/244 closed with no error code), they are not idle (median 87 commands), and
the target is answering normally (error signals in <60% of results for 89% of
Runs).  They fail because they have nothing concrete to send -- the corpus held
**6** payload entries, all ``unverified``, so every Run re-derived its payload
from a generic vulhub README.  Four runs of the same CVE README were even read
from the target's own ``docker-compose.yml`` because the README did not say
enough.

A payload recovered from a solved Run has three properties no crawled corpus
has: it ran against the same ``direct-ctf2`` platform, it is known to have
produced a flag there, and its failure modes are already in ``run_events`` next
to it.

What "the decisive command" means here.  Not the last command, and not the one
that printed the flag: the flag usually surfaces several steps after the
exploit that produced it (``0935613b`` reads it at seq 75, but only after a
signature was forged and replayed).  The extractor therefore reports the
*exploit-shaped* command nearest before the first observation of the flag, and
keeps the whole winning segment so a reader can see the chain.

Output is a review queue, not a corpus.  Every entry is written with
``trust_level: "candidate"`` and printed for a human to accept; promoting it to
``verified`` is a separate, explicit step.  A wrong payload that silently enters
the corpus is worse than no payload, because the Worker will then spend 87
commands re-trying it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from measure_first_exploit import classify_command  # noqa: E402

#: Placeholders that must be parameterised before a command is reusable.  Each
#: one is a value that differs per target instance, so leaving it literal would
#: make the payload useless on the next Run while looking perfectly valid.
_SCRUBBERS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Host names first, and **without** requiring a scheme.  Measured
    # 2026-11-05: a draft keyed on ``https?://...dasctf...`` missed
    # ``root@direct-ctf2.dasctf.com`` in an ssh command, so a real target host
    # sailed into the candidate file -- the exact class of leak this scrubber
    # exists to prevent.  Match the bare host and let the scheme ride along.
    (re.compile(r"\b(?:[0-9a-f]{6,}\.)?http-ctf2\.dasctf\.com(?::\d+)?"), "{{target_url}}"),
    (re.compile(r"\bdirect-ctf2\.dasctf\.com(?::\d+)?"), "{{target_url}}"),
    (re.compile(r"\b[a-z0-9-]*(?:ctf|dasctf)\.dasctf\.com(?::\d+)?"), "{{target_url}}"),
    (re.compile(r"\b[a-z0-9-]*(?:ctf2|ctf)\.dasctf\.com(?::\d+)?"), "{{target_url}}"),
    (re.compile(r"192\.168\.\d+\.\d+(?::\d+)?"), "{{host}}"),
    (re.compile(r"127\.0\.0\.1:\d+"), "{{host}}"),
    # A real external IP that appeared in a Run's command must not be carried
    # into the corpus: it is either the author's own host or an unrelated third
    # party, and publishing either would be wrong.  Seen once already
    # (``117.21.200.176`` in an ActiveMQ entry) before this pattern existed.
    (re.compile(r"\b(?!10\.|192\.168\.|127\.)(?:\d{1,3}\.){3}\d{1,3}\b"), "{{ip}}"),
    # A real flag must never reach the corpus or a log.
    (re.compile(r"[A-Za-z0-9_]{2,20}\{[0-9a-fA-F-]{8,}\}"), "{{flag}}"),
    (re.compile(r"flag\{[^}]{1,200}\}"), "{{flag}}"),
    (re.compile(r"CTF2\{[^}]{1,200}\}"), "{{flag}}"),
    # Per-run scratch files.
    (re.compile(r"/tmp/[A-Za-z0-9_.-]{4,}"), "/tmp/{{scratch}}"),
    (re.compile(r"webhook\.site/token/[0-9a-f-]{20,}"), "webhook.site/token/{{token}}"),
    # Session cookies and forged credentials.
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "{{api_key}}"),
)


#: Commands that matched an exploit shape only because the *tool name* was in
#: them, while carrying nothing replayable.  Measured 2026-11-05: 9 of the first
#: 43 candidates were this.
_NOT_A_PAYLOAD = re.compile(
    r"--help|--version|\bwhich\s|\bman\s|ls\s+-la\s+/(?:home|usr|opt)|"
    r"GeneratePayload\s+2>&1|\|\s*head\s+-\d+\s*$|\bgrep\s+-c\b",
    re.I,
)

#: The collector's historical clip on command text.  Traces recorded before the
#: 2026-11-05 fix cannot be replayed past this length, so they must not be
#: offered as payloads.  Kept as a constant rather than a magic number so the
#: reason survives the fix: this is an *archive* limit, not a live one.
_CLIPPED_AT = 300


def _scrub(text: str) -> tuple[str, list[str]]:
    """Replace per-instance values, reporting which substitutions happened."""

    found: list[str] = []
    out = text
    for pattern, replacement in _SCRUBBERS:
        if pattern.search(out):
            # Record the bare placeholder name.  ``strip("{}")`` was wrong here:
            # it turned ``/tmp/{{scratch}}`` into ``/tmp/{{scratch`` because the
            # string only *ends* with braces.
            found.extend(re.findall(r"\{\{(\w+)\}\}", replacement))
            out = pattern.sub(replacement, out)
    return out, found


def _iter_commands(rows) -> list[tuple[int, str, str]]:
    """(sequence, command, result_excerpt) for each tool call, in order.

    ``command`` lives on the ``tool`` row and the output on the ``tool_result``
    row, both under the same event type, so they are paired positionally: a
    ``tool`` row is followed by its ``tool_result``.
    """

    pairs: list[tuple[int, str, str]] = []
    pending: tuple[int, str] | None = None
    for row in rows:
        try:
            body = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            continue
        payload = body.get("payload") or {}
        kind = payload.get("kind")
        command = payload.get("command") or ""
        if kind == "tool":
            if pending is not None:
                # Previous call produced no result row (timeout/kill).
                pairs.append((pending[0], pending[1], ""))
            pending = (row["sequence"], command)
        elif kind == "tool_result":
            if pending is not None:
                pairs.append((pending[0], pending[1], payload.get("result_excerpt") or ""))
                pending = None
            else:
                pairs.append((row["sequence"], command, payload.get("result_excerpt") or ""))
    if pending is not None:
        pairs.append((pending[0], pending[1], ""))
    return pairs


def extract(limit: int, *, json_out: Path | None, show: int) -> int:
    import pymysql

    conn = pymysql.connect(
        host=os.getenv("APP_AUDIT_DB_HOST", "127.0.0.1"),
        port=int(os.getenv("APP_AUDIT_DB_PORT", "3307")),
        user=os.getenv("APP_AUDIT_DB_USER", "ctf_agent"),
        password=os.getenv("APP_AUDIT_DB_PASSWORD", "ctf_agent"),
        database=os.getenv("APP_AUDIT_DB_NAME", "ctf_agent"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )

    entries: list[dict] = []
    stats = {"runs": 0, "no_flag_in_events": 0, "no_exploit_before_flag": 0,
             "not_a_payload": 0, "too_short": 0, "clipped": 0, "extracted": 0}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT r.id, r.challenge_id, c.name, c.challenge_type,
                          c.flag_pattern, c.target_url,
                          JSON_UNQUOTE(JSON_EXTRACT(c.metadata_json,'$.arena_challenge_id')) AS arena_id
                   FROM solve_runs r
                   JOIN challenges c ON c.id = r.challenge_id
                   JOIN flag_candidates f ON f.run_id = r.id
                   WHERE r.status = 'COMPLETED_SOLVED'
                     AND f.review_state = 'VALID'
                   GROUP BY r.id, r.challenge_id, c.name, c.challenge_type,
                            c.flag_pattern, c.target_url, arena_id
                   ORDER BY r.created_at DESC
                   LIMIT %s""",
                (limit,),
            )
            runs = cur.fetchall()

            for run in runs:
                stats["runs"] += 1
                with conn.cursor() as c2:
                    c2.execute(
                        """SELECT candidate FROM flag_candidates
                           WHERE run_id = %s AND review_state = 'VALID'
                           ORDER BY created_at LIMIT 1""",
                        (run["id"],),
                    )
                    flag_row = c2.fetchone()
                    if not flag_row:
                        continue
                    flag = flag_row["candidate"]

                    c2.execute(
                        """SELECT sequence, payload_json FROM run_events
                           WHERE run_id = %s AND event_type = 'muteki.worker_command'
                           ORDER BY sequence""",
                        (run["id"],),
                    )
                    pairs = _iter_commands(c2.fetchall())

                if not pairs:
                    continue

                # Where did the flag first show up?
                hit_index = None
                for idx, (_seq, _cmd, excerpt) in enumerate(pairs):
                    if excerpt and flag in excerpt:
                        hit_index = idx
                        break
                if hit_index is None:
                    stats["no_flag_in_events"] += 1
                    continue

                # The decisive step: the nearest exploit-shaped command at or
                # before the flag observation.  Scanning backwards matters --
                # the flag often appears in a *read* (grep a file, print a
                # var) several steps after the exploit that planted it.
                decisive = None
                for idx in range(hit_index, max(-1, hit_index - 25), -1):
                    if classify_command(pairs[idx][1]) == "exploit":
                        decisive = idx
                        break
                if decisive is None:
                    stats["no_exploit_before_flag"] += 1
                    continue

                # Keep the chain: decisive step through the flag observation,
                # capped so one entry stays readable.
                segment = pairs[decisive : hit_index + 1][:8]
                raw_command = pairs[decisive][1]
                # Reject entries that are not payloads at all.  Nine of the first
                # 43 candidates were usage lookups (``ysoserial.GeneratePayload
                # 2>&1 | head -40``, ``--help``, ``which x``): they matched an
                # exploit-shape pattern because the *tool* name was there, but
                # there is nothing in them to replay.  A queue full of those
                # trains a reviewer to rubber-stamp, which is the failure mode
                # this whole file exists to avoid.
                if _NOT_A_PAYLOAD.search(raw_command):
                    stats["not_a_payload"] += 1
                    continue
                scrubbed_cmd, placeholders = _scrub(raw_command)
                # ``{{target_url}}`` is the *host[:port] only* -- the scrubber
                # replaces the hostname in place, so any surrounding scheme
                # survives.  An earlier version stripped ``http://`` off the
                # front, which produced 29 entries like
                # ``curl {{target_url}}/robots`` that no shell can run.  A
                # payload must be executable exactly as written.
                if "{{target_url}}" in scrubbed_cmd and not re.search(
                    r"https?://\{\{target_url\}\}", scrubbed_cmd
                ):
                    # Bare placeholder with no scheme anywhere before it: add one
                    # unless the host is being used as a bare host argument
                    # (``root@host``, ``host:port`` in a non-URL command).
                    if re.search(r"(?:curl|wget)\b[^|;]*?\{\{target_url\}\}", scrubbed_cmd):
                        scrubbed_cmd = scrubbed_cmd.replace("{{target_url}}", "http://{{target_url}}", 1)
                if len(scrubbed_cmd.strip()) < 60:
                    stats["too_short"] += 1
                    continue
                # Never emit a payload that was cut mid-statement.  The collector
                # used to clip commands at 300 chars (fixed 2026-11-05, see
                # ``muteki/solver/cli_driver.py``); any trace recorded before that
                # fix is still clipped, and an unrecorded 258-char stub is worse
                # than no entry at all — it looks like a payload and cannot run.
                if len(raw_command.rstrip()) >= _CLIPPED_AT:
                    stats["clipped"] += 1
                    continue
                chain = [
                    {"seq": seq, "command": _scrub(cmd)[0][:800]}
                    for seq, cmd, _ex in segment
                ]
                entries.append(
                    {
                        "kind": "poc",
                        "title": f"{run['name']} — decisive step",
                        "technique": _guess_technique(pairs[decisive][1], run["name"]),
                        "payload_template": scrubbed_cmd[:2000],
                        "placeholders": sorted(set(placeholders)),
                        "preconditions": [],
                        "verification_method": [
                            "Re-run the command with placeholders filled; success is the "
                            "response containing the challenge's flag pattern."
                        ],
                        "known_failures": [],
                        "steps": [c["command"][:300] for c in chain],
                        "post_exploitation": [],
                        "target_stack": [run["challenge_type"]] if run["challenge_type"] else [],
                        "provenance": {
                            "source_type": "own-solved-run",
                            "run_id": run["id"],
                            "challenge": run["name"],
                            "arena_challenge_id": run["arena_id"],
                            "decisive_sequence": pairs[decisive][0],
                            "flag_sequence": pairs[hit_index][0],
                            "substitutions": sorted(set(placeholders)),
                        },
                        # Never "verified": a human promotes it after review.
                        "trust_level": "candidate",
                        "published_at": None,
                    }
                )
                stats["extracted"] += 1
    finally:
        conn.close()

    print(
        f"扫描 {stats['runs']} 个已解 Run → 提取 {stats['extracted']} 条候选 payload\n"
        f"  跳过：flag 不在 worker 输出里 {stats['no_flag_in_events']} 条；"
        f"flag 前找不到利用形态命令 {stats['no_exploit_before_flag']} 条；"
        f"不是 payload（查用法/帮助）{stats['not_a_payload']} 条；过短 {stats['too_short']} 条；"
        f"**采集端截断 {stats['clipped']} 条**"
    )
    if stats["clipped"]:
        print(
            f"  注：这 {stats['clipped']} 条是 2026-11-05 修复采集截断之前记录的轨迹，"
            f"命令在 {_CLIPPED_AT} 字符处被切断，无法重放。修复只对新 Run 生效。"
        )

    if show and entries:
        print(f"\n=== 前 {min(show, len(entries))} 条（人工审核用）===")
        for entry in entries[:show]:
            prov = entry["provenance"]
            print(f"\n[{entry['technique']}] {entry['title'][:60]}")
            print(f"  来源 run={prov['run_id'][:8]} seq={prov['decisive_sequence']}→{prov['flag_sequence']}")
            print(f"  占位符: {', '.join(entry['placeholders']) or '(无)'}")
            print(f"  命令: {entry['payload_template'][:220]}")

    if json_out:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + ("\n" if entries else ""),
            encoding="utf-8",
        )
        print(f"\n写入 {json_out}（{len(entries)} 条，trust_level=candidate，需人工审核后再合并）")
    return 0 if entries else 1


def _guess_technique(command: str, name: str) -> str:
    """A coarse label, guessed from the payload shape and then the challenge name.

    Order matters: the *payload* is stronger evidence than the title, because a
    challenge called "SSRF Me" can still have been solved through a signature
    forgery, and filing it under ``ssrf`` would then never retrieve the thing
    that actually worked.

    Deliberately crude and overridable: the reviewer sets the real technique when
    promoting the entry.  A mis-filed payload is never retrieved by the technique
    axis, which is worse than leaving an obvious label.
    """

    c = command.casefold()

    # --- payload shape first -------------------------------------------------
    if "sshpass" in c or re.search(r"\bssh\s+[^\s]*@", c):
        return "credential-abuse"
    if "ysoserial" in c or "rmiregistry" in c or "marshalsec" in c:
        return "deserialization"
    if "gremlin" in c:
        return "graph-query-injection"
    if re.search(r"%!ps-adobe|setpagedevice", c):
        return "image-parser-rce"
    if re.search(r"jenkins-cli\.jar[^\n]*-http\s+connect-node", c):
        return "arbitrary-file-read"
    if "#set (" in c or "runtime" in c and "${" in c:
        return "ssti"
    if "jndi:" in c:
        return "jndi-injection"
    if "php://filter" in c:
        return "lfi"
    if re.search(r"(?:file=|path=|param=|/|=)\.\./\.\./", c):
        return "path-traversal"
    if re.search(r"union\s+(?:all\s+)?select", c):
        return "sqli"
    if re.search(r"(?:sleep|benchmark)\s*\(\s*\d+", c):
        return "sqli"
    if "php://input" in c or "data://" in c:
        return "rce"
    if re.search(r"<!entity|system\s+[\"']file://", c):
        return "xxe"
    if re.search(r"\.phtml|\.phar\b|\.jsp['\"]?\s*$", c):
        return "file-upload"
    if "169.254.169.254" in c:
        return "ssrf"

    # --- then the challenge name --------------------------------------------
    # Only when the payload gave no signal at all.  A title like "[De1CTF]SSRF
    # Me" is a hint about the intended flaw, not evidence about what actually
    # worked -- that Run solved it by forging an md5 signature over
    # ``param=/proc/self/cwd/flag.txt``, so filing it as ``ssrf`` would mean the
    # entry is never retrieved by the technique that mattered.
    name_low = name.casefold()
    for key, label in (
        ("ssti", "ssti"), ("struts", "ssti"), ("spel", "ssti"),
        ("sqli", "sqli"), ("sql", "sqli"),
        ("lfi", "lfi"), ("include", "lfi"), ("filemanager", "lfi"),
        ("upload", "file-upload"), ("xxe", "xxe"), ("deser", "deserialization"),
        ("jwt", "jwt-forgery"), ("token", "jwt-forgery"),
        ("rce", "rce"), ("exec", "rce"),
    ):
        if key in name_low:
            return label
    return "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=200, help="how many solved Runs to scan")
    parser.add_argument("--show", type=int, default=5, help="print this many entries for review")
    parser.add_argument(
        "--out",
        default="data/knowledge/candidates/payload-candidates.jsonl",
        help="review queue path (must live OUTSIDE data/knowledge/*.jsonl -- that glob is the live corpus loader and rejects this schema)",
    )
    args = parser.parse_args()
    return extract(args.limit, json_out=Path(args.out), show=args.show)


if __name__ == "__main__":
    raise SystemExit(main())
