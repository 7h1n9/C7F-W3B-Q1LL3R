"""Is a failed Run stuck on one hypothesis, or just short of information?

The distinction decides what to build.  If Runs fail because they never saw
anything useful, more knowledge (bigger recipe corpus, a fingerprint index,
on-demand ``kb lookup``) is the answer.  If they fail because they saw plenty
and then spent the rest of the budget re-cutting the same payload, more
knowledge makes it *worse* -- every extra variant is one more thing to try
inside a direction that was already dead.

This measures the second case mechanically: map each tool command to one
attack-surface family, then look at how much of the *back half* of the Run sits
in a single family, and how many new facts that back half produced.

The 11 Runs of 2026-10-28 20:10-21:03 UTC all ended UNSOLVED with healthy
infrastructure, so they are the right population: nothing else explains them.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
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

#: Attack-surface families, matched against the command text only.  Ordered:
#: the first family that matches wins, so the specific signals come before the
#: generic ones.  ``recon`` is last on purpose -- a ``curl -i /`` that only
#: fetches the home page is reconnaissance, not an exploit attempt.
FAMILIES: list[tuple[str, re.Pattern[str]]] = [
    # Bookkeeping against the shared graph is neither recon nor exploitation,
    # and it turned out to be a large share of the sampled commands because the
    # Blackboard was broken (``no such table: events``, 404 times).  Counting it
    # as "recon" would have overstated reconnaissance by ~20%.
    ("board", re.compile(
        r"(blackboard\.py|upstream_shared_graph\.db|\.muteki_blackboard|"
        r"read-facts|read-deadends|read-routes|write-fact|write-deadend)", re.I)),
    # Building or editing an exploit script is an attack action even though the
    # command text is ``python3 - <<EOF`` / ``cat > p.py``.  Kept *after* the
    # concrete payload families so a heredoc containing ``<script>`` or
    # ``__class__`` still counts as xss/ssti; it only catches the remainder.
    ("xss", re.compile(
        r"(<script|onerror=|onload=|javascript:|String\.fromCharCode|"
        r"document\.cookie|alert\(1\)|<\s*img\s+src\s*=\s*x)", re.I)),
    ("ssti", re.compile(
        r"(\{\{.*?\}\}|\{%|%\}\$|__class__|__mro__|__subclasses__|"
        r"lipsum|cycler|joiner|config\.items|url_for|get_flashed)", re.I)),
    ("sqli", re.compile(
        r"(union\s+select|\bor\s+1\s*=\s*1|and\s+1\s*=\s*1|sleep\(\d|"
        r"extractvalue|updatexml|information_schema|sqlmap|floor\(rand|"
        r"benchmark\(|--\s*$|#\s*$|'\s*or\s*')", re.I)),
    ("lfi", re.compile(
        r"(\.\./\.\.|/etc/passwd|php://|file://|data://|expect://|"
        r"zip://|phar://|%00|\.\.%2f)", re.I)),
    ("deser", re.compile(
        r"(unserialize|__wakeup|__destruct|__construct|O:\d+:\"|"
        r"a:\d+:\{|s:\d+:\"|pickle|yaml\.load|readObject)", re.I)),
    ("ssrf", re.compile(
        r"(url=https?://|target=https?://|dest=https?://|redirect=https?://|"
        r"uddiexplorer|169\.254\.169\.254|gopher://|dict://|"
        r"\|http://|127\.0\.0\.1:\d)", re.I)),
    ("upload", re.compile(
        r"(-F\s+[\"']?\w+=@|curl.*-F\s|\.php['\"]?\s*;|Content-Type:\s*image|"
        r"GIF89a|\.htaccess|multipart/form-data|filename=)", re.I)),
    ("rce", re.compile(
        r"(;\s*(id|whoami|ls|cat)\b|\$\(|\|\s*(id|whoami|cat)\b|"
        r"bash\s+-c|/bin/sh|system\(|passthru|shell_exec|exec\(|eval\()", re.I)),
    ("crypto", re.compile(
        r"(hashlib|md5\(|sha1\(|base64\s+-d|openssl|rsa|aes|xor|"
        r"z3\.|sympy|factorint)", re.I)),
    ("fuzz", re.compile(
        r"(ffuf|gobuster|dirb\b|wfuzz|nikto|nuclei|sqlmap)", re.I)),
    # Heredoc script work is an attack action, but only identified after every
    # concrete payload family has had its chance -- otherwise a heredoc holding
    # ``<script>`` would be scored as "script" and the XSS direction would look
    # smaller than it is.
    ("script", re.compile(
        r"(cat\s*>\s*\S+\.(py|js|sh|php)|python3?\s+-\s*<<|"
        r"import\s+requests|from\s+urllib|http\.client|\.replace\(\")", re.I)),
    ("recon", re.compile(r".*", re.S)),
]

CMD_SQL = """
SELECT payload_json
FROM run_events
WHERE run_id = %s
  AND event_type = 'muteki.worker_command'
  AND JSON_EXTRACT(payload_json, '$.payload.kind') = 'tool'
ORDER BY sequence
"""

FACT_SQL = """
SELECT COUNT(*) FROM run_events
WHERE run_id = %s AND event_type = 'muteki.fact_added'
"""

NAME_SQL = """
SELECT c.name FROM solve_runs r JOIN challenges c ON c.id = r.challenge_id
WHERE r.id = %s
"""


def family(cmd: str) -> str:
    for name, pattern in FAMILIES:
        if pattern.search(cmd):
            return name
    return "recon"


def analyse(cmds: list[str], facts: int) -> dict:
    n = len(cmds)
    if not n:
        return {"commands": 0}
    fams = [family(c) for c in cmds]
    cut = int(n * 0.6)

    # Blackboard bookkeeping is overhead, not a direction: it must never win
    # "dominant family", or a Run that spent its back half fighting a broken
    # graph would be reported as "locked on recon".
    def combat(seq: list[str]) -> list[str]:
        return [f for f in seq if f != "board"]

    back = combat(fams[cut:])
    if not back:
        back = ["recon"]
    back_counter = Counter(back)
    top, top_n = back_counter.most_common(1)[0]

    # A direction is "locked" when the back half keeps returning to one family
    # that already dominated the first part -- not merely when one family is
    # common because the box is a single-page app.
    first = combat(fams[:cut]) or ["recon"]
    first_counter = Counter(first)
    first_top, first_n = first_counter.most_common(1)[0]

    return {
        "commands": n,
        "facts": facts,
        "back_len": len(back),
        "back_top": top,
        "back_share": top_n / max(len(back), 1),
        "first_top": first_top,
        "first_share": first_n / max(len(first), 1),
        "same_family": top == first_top,
        "distinct_families": len({f for f in fams if f not in ("recon", "board")}),
        "recon_share": fams.count("recon") / n,
        "board_share": fams.count("board") / n,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="append", default=[], required=True)
    args = ap.parse_args(argv)

    conn = pymysql.connect(**DB)
    try:
        cur = conn.cursor()
        print(f"{'challenge':<26}{'cmd':>5}{'facts':>6}{'back':>5}"
              f"{'top family':>13}{'share':>7}{'same?':>6}{'fam#':>5}"
              f"{'recon%':>7}{'board%':>7}")
        print("-" * 87)
        locked = 0
        rows = []
        for run_id in args.run:
            cur.execute(CMD_SQL, (run_id,))
            cmds = []
            for (payload_json,) in cur.fetchall():
                cmd = (json.loads(payload_json).get("payload") or {}).get("command") or ""
                if cmd.strip():
                    cmds.append(cmd)
            cur.execute(FACT_SQL, (run_id,))
            facts = cur.fetchone()[0]
            cur.execute(NAME_SQL, (run_id,))
            row = cur.fetchone()
            name = (row[0] if row else "?")[:24]

            st = analyse(cmds, facts)
            if not st.get("commands"):
                continue
            is_locked = st["same_family"] and st["back_share"] >= 0.55
            locked += is_locked
            rows.append((name, st, is_locked))
            print(f"{name:<26}{st['commands']:>5}{st['facts']:>6}"
                  f"{st['back_len']:>5}{st['back_top']:>13}"
                  f"{st['back_share'] * 100:>6.0f}%"
                  f"{'YES' if is_locked else '-':>6}"
                  f"{st['distinct_families']:>5}"
                  f"{st['recon_share'] * 100:>6.0f}%"
                  f"{st['board_share'] * 100:>6.0f}%")

        n = max(len(rows), 1)
        print("-" * 87)
        print(f"前半程主导家族 = 后半程主导家族 且 后半程占比 ≥55% 的运行："
              f"{locked}/{n} ({locked / n * 100:.0f}%)")
        avg_recon = sum(r[1]["recon_share"] for r in rows) / n
        avg_board = sum(r[1]["board_share"] for r in rows) / n
        avg_fam = sum(r[1]["distinct_families"] for r in rows) / n
        print(f"平均纯侦察命令占比 {avg_recon * 100:.0f}%")
        print(f"平均黑板相关命令占比 {avg_board * 100:.0f}%")
        print(f"平均尝试过的攻击家族数 {avg_fam:.1f}")
        per_cmd = sum(r[1]["facts"] for r in rows) / max(
            sum(r[1]["commands"] for r in rows), 1)
        print(f"每 100 条命令产出事实数 {per_cmd * 100:.1f}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
