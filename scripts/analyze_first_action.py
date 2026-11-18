"""Does the first command predict the outcome?

The project has a long-standing belief that a "FIRST ACTION" -- an exploit
command injected before the worker starts doing generic reconnaissance --
lifts the solve rate by a large factor.  That claim came from comparing
challenges that *had* a curated recipe with challenges that did not, which is
a selection effect rather than an intervention.

This script asks the observable version of the question, using only data we
already have: for every archived run, look at the first command it issued and
cross that against the final status.  It cannot establish causality -- runs
that open with an exploit were probably easier or better documented to begin
with -- but it does establish whether the "warming up costs us the run" story
is even consistent with the traces.

Two judgements are made about the first command:

``touches``   the command references the target at all.  The export redacts
              the target origin to ``{{target_url}}``, so this is a substring
              test on the placeholder plus bare ``http(s)://`` literals.
``exploit``   the command looks like an actual attempt (see ``EXPLOIT_RE``),
              not an inventory or a banner grab.

Read-only: this script never writes to the database and never mutates the
export.  It is deliberately dependency-free so it can run without the backend.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _metrics_boot import record_analysis  # noqa: E402  (also puts backend/ on sys.path)

DEFAULT_SRC = ROOT / "data" / "training" / "transitions-20261103T161417Z.jsonl"

# "Already swinging" shapes: a method'd request, a payload, script execution,
# an upload.  Kept identical in spirit to the classifier used by
# ``analyze_truncation_points.py`` so the two reports stay comparable.
EXPLOIT_RE = re.compile(
    r"(--data|--form|POST\s|GET\s+/|sqlmap|python3?\s|perl\s|bash\s+-c|nc\s|"
    r"wget\s|curl\s+-X\s|\.php\?|flag\{|/bin/sh|base64\s+-d|ysoserial)",
    re.I,
)
# Commands that only look at the box from the outside.
RECON_RE = re.compile(
    r"\b(ls|cat|head|tail|find|rg|grep|file|whoami|id|uname|nmap|dirb|gobuster|"
    r"whatweb|curl\s+-I|robots\.txt)\b",
    re.I,
)
TARGET_PLACEHOLDER = "{{target_url}}"
BARE_URL_RE = re.compile(r"https?://(?!127\.0\.0\.1|localhost)[^\s'\"<>]+", re.I)

# Status vocabulary as exported by ``transitions.py``.  Note that a naive
# ``"SOLVED" in status`` test would count COMPLETED_UNSOLVED as a solve --
# the export spells the negative case as a single token, so match exactly.
SOLVED_STATUSES = {"COMPLETED_SOLVED", "SOLVED"}
ABORTED_STATUSES = {"FAILED_ENGINE", "TIMEOUT"}


def classify(command: str) -> str:
    if EXPLOIT_RE.search(command):
        return "exploit"
    if RECON_RE.search(command):
        return "recon"
    return "other"


def touches_target(command: str) -> bool:
    if TARGET_PLACEHOLDER in command:
        return True
    return bool(BARE_URL_RE.search(command))


def load_runs(src: Path, limit: int = 0) -> dict[str, list[dict]]:
    """Group transitions by run, preserving file order."""

    runs: dict[str, list[dict]] = {}
    with src.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                # Every row is validated at export time; a bad line here would
                # mean the export itself is corrupt, so say so rather than
                # silently shrinking the denominator.
                print("WARNING: unparseable transition row, skipped", file=sys.stderr)
                continue
            runs.setdefault(row["run_id"], []).append(row)
            if limit and len(runs) >= limit:
                break
    return runs


def first_command(rows: list[dict]) -> str | None:
    """The first command the run actually issued, in sequence order."""

    for row in sorted(rows, key=lambda r: int(r.get("sequence") or 0)):
        command = str((row.get("action") or {}).get("command") or "").strip()
        if command:
            return command
    return None


def bucket(status: str) -> str:
    if status in SOLVED_STATUSES:
        return "solved"
    if status in ABORTED_STATUSES:
        return "aborted"
    return "unsolved"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, default=DEFAULT_SRC)
    parser.add_argument("--limit", type=int, default=0, help="cap number of runs")
    parser.add_argument("--examples", type=int, default=3, help="sample lines per bucket")
    args = parser.parse_args()

    if not args.src.is_file():
        print(f"数据源不存在: {args.src}", file=sys.stderr)
        return 2

    runs = load_runs(args.src, args.limit)
    print(f"数据源 {args.src.name} | Run {len(runs)}")

    # run_id -> (bucket, touches, kind, command)
    classified: dict[str, tuple[str, bool, str, str]] = {}
    no_command = 0
    for run_id, rows in runs.items():
        command = first_command(rows)
        if command is None:
            no_command += 1
            continue
        status = str(((rows[0].get("labels") or {}).get("final_status")) or "")
        classified[run_id] = (bucket(status), touches_target(command), classify(command), command)

    if not classified:
        print("没有任何 Run 含可执行命令，无法分析。", file=sys.stderr)
        return 2

    status_counts = collections.Counter(v[0] for v in classified.values())
    print(f"有命令的 Run {len(classified)} | 无命令 {no_command}")
    print("状态分布:", dict(status_counts))

    # ---- headline: first command touches the target at all ----------------
    print("\n=== 首个命令是否接触目标 vs 最终状态 ===")
    table: dict[bool, collections.Counter] = collections.defaultdict(collections.Counter)
    for status, touches, _kind, _cmd in classified.values():
        table[touches][status] += 1
    for touches in (True, False):
        counts = table[touches]
        total = sum(counts.values())
        label = "接触目标" if touches else "未接触目标"
        solved = counts["solved"]
        rate = 100 * solved / total if total else 0.0
        print(
            f"  {label:<8} n={total:>3} | 解出 {solved:>3} ({rate:5.1f}%)"
            f" | 未解 {counts['unsolved']:>3} | 中断 {counts['aborted']:>3}"
        )

    # ---- second cut: first command is already an exploit ------------------
    print("\n=== 首个命令形态 vs 最终状态 ===")
    for kind in ("exploit", "recon", "other"):
        subset = [v for v in classified.values() if v[2] == kind]
        if not subset:
            continue
        solved = sum(1 for v in subset if v[0] == "solved")
        touch = sum(1 for v in subset if v[1])
        print(
            f"  {kind:<8} n={len(subset):>3} | 解出 {solved:>3} ({100 * solved / len(subset):5.1f}%)"
            f" | 其中接触目标 {touch:>3}"
        )

    # ---- how many steps of warm-up before the target is touched at all ----
    warmup: list[int] = []
    for run_id, (_status, _touches, _kind, _cmd) in classified.items():
        for position, row in enumerate(sorted(runs[run_id], key=lambda r: int(r.get("sequence") or 0))):
            command = str((row.get("action") or {}).get("command") or "").strip()
            if command and touches_target(command):
                warmup.append(position)
                break
    if warmup:
        warmup.sort()
        n = len(warmup)
        print(
            f"\n首次接触目标前的命令数: 中位 {warmup[n // 2]} | 平均 {sum(warmup) / n:.1f}"
            f" | 第 1 条就打目标 {sum(1 for w in warmup if w == 0)}/{len(classified)}"
            f" ({100 * sum(1 for w in warmup if w == 0) // len(classified)}%)"
            f" | 从未接触 {len(classified) - n}"
        )

    # ---- samples ---------------------------------------------------------
    print("\n=== 样本（每桶最多 %d 条首命令）===" % args.examples)
    shown: collections.Counter = collections.Counter()
    for run_id, (status, touches, kind, command) in classified.items():
        key = (status, kind)
        if shown[key] >= args.examples:
            continue
        shown[key] += 1
        short = command.replace("\n", " ")[:110]
        print(f"  [{status}/{kind}/touch={touches}] {run_id[:8]}: {short}")

    # Provenance row: these numbers end up in the plan document, so they have to
    # be recomputable without re-deriving which filter produced them.
    record_analysis(
        "analyze_first_action",
        f"transitions jsonl: {args.src} | 全量读取",
        {
            "runs_with_command": len(classified),
            "status_histogram": dict(status_counts),
            "first_command_touches_target": {
                str(touches): dict(table[touches]) for touches in (True, False)
            },
            "warmup_median_commands": (warmup[len(warmup) // 2] if warmup else None),
            "first_command_hits_target": sum(1 for w in warmup if w == 0),
        },
        filters={"limit": args.limit or None, "src": args.src.name},
        data_window=f"{args.src.name} 全量",
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
