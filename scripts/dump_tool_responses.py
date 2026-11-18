"""Dump the raw *responses* a Worker saw, with the commands stripped out.

Why this exists
---------------
``learned-boxes.jsonl`` distils ``steps``, which is narrative: it mixes "what I
did" with "what I saw" in one string and cannot be separated afterwards.  That
made offline evaluation of the signal index inconclusive -- the observable
surface it measured was the surface the distiller chose to write down, not the
surface the box actually exposed (median 5 signals per entry, 23/99 with no
technique label at all).

``muteki.worker_command`` events with ``kind='tool_result'`` hold
``payload.result_excerpt``: the raw stdout/stderr, no action attached.  This
script pulls those out so the question "does the response alone carry enough to
locate a technique?" can be judged against actual bytes instead of a summary.

Usage::

    python scripts/dump_tool_responses.py --limit 30 \
        --run <run_id> --run <run_id> ... --out .probe/response_trace.md
"""

from __future__ import annotations

import argparse
import json
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
SELECT payload_json, sequence
FROM run_events
WHERE run_id = %s
  AND event_type = 'muteki.worker_command'
  AND JSON_EXTRACT(payload_json, '$.payload.kind') = 'tool_result'
ORDER BY sequence
LIMIT %s
"""

NAME_SQL = """
SELECT c.name FROM solve_runs r JOIN challenges c ON c.id = r.challenge_id
WHERE r.id = %s
"""


def fetch(cur, run_id: str, limit: int) -> list[str]:
    cur.execute(SQL, (run_id, limit))
    out = []
    for payload_json, _seq in cur.fetchall():
        row = json.loads(payload_json)
        text = (row.get("payload") or {}).get("result_excerpt") or ""
        out.append(text)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="append", default=[], required=True)
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument(
        "--max-chars",
        type=int,
        default=1600,
        help="per-response clip; long tails are usually repeated noise",
    )
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)

    conn = pymysql.connect(**DB)
    try:
        cur = conn.cursor()
        blocks: list[str] = []
        for i, run_id in enumerate(args.run):
            cur.execute(NAME_SQL, (run_id,))
            row = cur.fetchone()
            name = row[0] if row else "?"
            bodies = fetch(cur, run_id, args.limit)
            label = chr(ord("A") + i)

            blocks.append(f"\n\n## Case {label} -- {name}  (run {run_id[:8]})\n")
            blocks.append(f"_{len(bodies)} responses, first {args.limit} of the Run_\n")
            for j, text in enumerate(bodies, 1):
                if not text.strip():
                    blocks.append(f"\n### [{j}] (empty)\n")
                    continue
                clipped = text[: args.max_chars]
                more = "" if len(text) <= args.max_chars else (
                    f"\n... [+{len(text) - args.max_chars} chars]"
                )
                blocks.append(f"\n### [{j}]\n```\n{clipped}{more}\n```\n")
    finally:
        conn.close()

    body = "".join(blocks)
    if args.out:
        target = Path(args.out)
        if not target.is_absolute():
            target = ROOT / target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        print(f"wrote {target}  ({len(body)} chars)")
    else:
        print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
