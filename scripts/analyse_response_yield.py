"""How much of what a Worker *sees* is actually new information.

The point of this probe is to separate two failure modes that look identical in
the final "unsolved" verdict:

* the Worker never saw anything that identifies the technique, or
* the Worker saw it and then buried it under repeats and housekeeping noise.

``learned-boxes.jsonl`` cannot answer that: it distils ``steps``, which merges
action and observation into one narrative.  ``tool_result`` events hold the raw
stdout, so the counts here are about bytes on the wire, not about an LLM's
summary of them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parents[1]

DB = {
    "host": "127.0.0.1",
    "port": 3307,
    "user": "ctf_agent",
    "password": "ctf_agent",
    "database": "ctf_agent",
    "charset": "utf8mb4",
}

SQL = """
SELECT payload_json
FROM run_events
WHERE run_id = %s
  AND event_type = 'muteki.worker_command'
  AND JSON_EXTRACT(payload_json, '$.payload.kind') = 'tool_result'
ORDER BY sequence
"""

#: Outputs that carry no observation about the target at all.  Kept narrow on
#: purpose: ``ls`` of the workspace and CLI bookkeeping are noise, but an empty
#: response from the *target* is a real datum (a 404 body that is blank still
#: tells you the route is dead), so those are counted separately.
HOUSEKEEPING = re.compile(
    r"("
    r"^total \d+\s*\n(d[-rwx]{9}.*\n?)+|"          # local ls of the workspace
    r"^\(Bash completed with no output\)$"
    r")"
)

#: Headers whose value changes on every request.  Leaving them in makes two
#: identical bodies hash differently -- that is why the earlier command-level
#: dedup saw only 6/215 repeats while a Worker was plainly re-reading the same
#: page.  Stripped before hashing, not before display.
VOLATILE = re.compile(
    r"^(Date|Expires|Set-Cookie|X-Request-Id|Etag|Last-Modified):.*$",
    re.IGNORECASE | re.MULTILINE,
)


def norm(text: str) -> str:
    """Collapse whitespace and drop per-request noise before hashing."""
    return re.sub(r"\s+", " ", VOLATILE.sub("", text)).strip()


def analyse(rows: list[str]) -> dict:
    seen: dict[str, int] = {}
    dup_runs = 0
    max_streak = 0
    streak = 0
    last = ""
    noise = 0
    for text in rows:
        key = hashlib.sha1(norm(text).encode("utf-8", "ignore")).hexdigest()
        if key == last:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
            last = key
        if key in seen:
            dup_runs += 1
        seen[key] = seen.get(key, 0) + 1
        if HOUSEKEEPING.search(text.strip()) or not text.strip():
            noise += 1

    unique = len(seen)
    return {
        "responses": len(rows),
        "unique": unique,
        "duplicate": dup_runs,
        "dup_rate": dup_runs / max(len(rows), 1),
        "max_consecutive_repeat": max_streak,
        "housekeeping": noise,
        "housekeeping_rate": noise / max(len(rows), 1),
        "useful_estimate": len(rows) - dup_runs - noise,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="append", default=[], required=True)
    ap.add_argument("--limit", type=int, default=0, help="0 = whole run")
    args = ap.parse_args(argv)

    conn = pymysql.connect(**DB)
    try:
        cur = conn.cursor()
        print(f"{'run':<10}{'resp':>7}{'uniq':>7}{'dup':>7}{'dup%':>8}"
              f"{'streak':>8}{'noise%':>8}{'useful':>8}")
        print("-" * 63)
        total = {"responses": 0, "duplicate": 0, "housekeeping": 0,
                 "unique": 0, "useful_estimate": 0}
        for run_id in args.run:
            cur.execute(SQL, (run_id,))
            rows = []
            for (payload_json,) in cur.fetchall():
                row = json.loads(payload_json)
                rows.append((row.get("payload") or {}).get("result_excerpt") or "")
            if args.limit:
                rows = rows[: args.limit]
            st = analyse(rows)
            for k in total:
                total[k] += st[k]
            print(f"{run_id[:8]:<10}{st['responses']:>7}{st['unique']:>7}"
                  f"{st['duplicate']:>7}{st['dup_rate'] * 100:>7.1f}%"
                  f"{st['max_consecutive_repeat']:>8}"
                  f"{st['housekeeping_rate'] * 100:>7.1f}%"
                  f"{st['useful_estimate']:>8}")
        n = max(total["responses"], 1)
        print("-" * 63)
        print(f"{'TOTAL':<10}{total['responses']:>7}{total['unique']:>7}"
              f"{total['duplicate']:>7}{total['duplicate'] / n * 100:>7.1f}%"
              f"{'':>8}{total['housekeeping'] / n * 100:>7.1f}%"
              f"{total['useful_estimate']:>8}")
        print()
        print(f"重复响应占比       {total['duplicate'] / n * 100:.1f}%")
        print(f"本地杂务占比       {total['housekeeping'] / n * 100:.1f}%")
        print(f"含新信息的响应估算 {total['useful_estimate'] / n * 100:.1f}%")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
