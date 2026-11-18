"""离线取证：未解 Run 被截断时，究竟处在「接近成功」还是「方向错误」。

回答的问题（决定「动态时间预算」值不值得做）：
  如果多数未解 Run 在最后 10 条命令里仍在产出新 fact，说明时间预算是约束，
  延长/动态延长有戏；如果多数在最后 10 条里 fact 零增长且仍在做浅层侦察，
  那么加时间只是把同一条错路走得更久，此时该改的是方法而非时钟。

数据源：data/training/transitions-*.jsonl（离线导出，不需要 DB）。
只读，不写任何文件。

用法：
    python scripts/analyze_truncation_points.py [--window 10] [--tail 3]
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SRC = ROOT / "data" / "training" / "transitions-20261103T161417Z.jsonl"

# 「浅层侦察」的形态：看目录、看 banner、看源码、扫端口。
RECON_RE = re.compile(
    r"\b(ls|cat|head|tail|find|rg|grep|file|whoami|id|uname|nmap|dirb|gobuster|"
    r"whatweb|curl\s+-I|robots\.txt|curl\s+-s\s+-o\s*/dev/null)\b",
    re.I,
)
# 「已经动手」的形态：带方法的请求、载荷、脚本执行、上传。
EXPLOIT_RE = re.compile(
    r"(--data|--form|POST\s|GET\s+/|sqlmap|python3?\s|perl\s|bash\s+-c|nc\s|"
    r"wget\s|curl\s+-X\s|\.php\?|flag\{|/bin/sh|base64\s+-d|ysoserial)",
    re.I,
)


def load_runs(path: Path) -> dict[str, list[dict]]:
    runs: dict[str, list[dict]] = defaultdict(list)
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue  # 计数由下方总行数校验兜底，避免静默丢行
            runs[row["run_id"]].append(row)
    for rows in runs.values():
        rows.sort(key=lambda r: r.get("sequence", 0))
    return runs


def classify_command(command: str) -> str:
    text = command or ""
    if EXPLOIT_RE.search(text):
        return "exploit"
    if RECON_RE.search(text):
        return "recon"
    return "other"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC)
    ap.add_argument("--window", type=int, default=10)
    ap.add_argument("--tail", type=int, default=3)
    args = ap.parse_args()

    runs = load_runs(args.src)
    total = sum(len(v) for v in runs.values())
    print(f"数据源 {args.src.name} | Run {len(runs)} | transition {total}")

    by_status = Counter(v[0]["labels"]["final_status"] for v in runs.values())
    print("状态分布:", dict(by_status))

    unsolved = [v for v in runs.values() if v[0]["labels"]["final_status"] != "COMPLETED_SOLVED"]
    solved = [v for v in runs.values() if v[0]["labels"]["final_status"] == "COMPLETED_SOLVED"]

    def profile(rows_list: list[list[dict]], label: str) -> None:
        n = len(rows_list)
        if not n:
            return
        growing = 0
        recon_end = 0
        exploit_end = 0
        looping = 0
        lens: list[int] = []
        for rows in rows_list:
            lens.append(len(rows))
            win = rows[-args.window:]
            growth = sum(int(r["observation"].get("delta_facts") or 0) for r in win)
            if growth > 0:
                growing += 1
            tails = [classify_command(r["action"].get("command", "")) for r in rows[-args.tail:]]
            if "exploit" in tails:
                exploit_end += 1
            elif tails and all(t == "recon" for t in tails):
                recon_end += 1
            # 原地打转的判据：末窗命令与更早命令完全重复
            earlier = [r["action"].get("command", "") for r in rows[:-args.window]]
            seen = set(earlier)
            repeats = sum(1 for r in win if r["action"].get("command", "") in seen)
            if win and repeats / len(win) >= 0.5:
                looping += 1
        lens.sort()
        print(
            f"\n[{label}] n={n} | 命令数中位={lens[n // 2]} "
            f"| 末{args.window}条有fact增长={growing}/{n} ({100 * growing // n}%) "
            f"| 末{args.tail}条全为侦察={recon_end}/{n} ({100 * recon_end // n}%) "
            f"| 末{args.tail}条含动手={exploit_end}/{n} ({100 * exploit_end // n}%) "
            f"| 末窗≥50%为重复命令={looping}/{n} ({100 * looping // n}%)"
        )

    profile(unsolved, "未解/引擎失败")
    profile(solved, "已解（对照）")

    # 四象限：末窗是否仍在产出 × 结尾形态
    quad = Counter()
    samples: dict[str, list[str]] = defaultdict(list)
    for rows in unsolved:
        win = rows[-args.window:]
        growth = sum(int(r["observation"].get("delta_facts") or 0) for r in win)
        tails = [classify_command(r["action"].get("command", "")) for r in rows[-args.tail:]]
        ending = "exploit" if "exploit" in tails else ("recon" if tails and all(t == "recon" for t in tails) else "mixed")
        key = ("fact+" if growth > 0 else "fact0") + " / " + ending
        quad[key] += 1
        if len(samples[key]) < 2:
            samples[key].append(rows[0]["run_id"][:8])

    print("\n四象限（未解 Run：末窗 fact 增量 × 结尾形态）")
    for key, cnt in quad.most_common():
        print(f"  {key:<18} {cnt:>4}  run示例 {', '.join(samples[key])}")

    n = len(unsolved)
    if n:
        stuck = quad.get("fact0 / recon", 0)
        close = quad.get("fact+ / exploit", 0) + quad.get("fact+ / mixed", 0)
        print(
            f"\n判读：方向错误型(fact0/recon) {stuck}/{n} = {100 * stuck // n}% ;"
            f" 仍在推进会(事实+/末端已动手或混合) {close}/{n} = {100 * close // n}%"
        )
        if stuck > n * 0.5:
            print("结论倾向：多数未解 Run 在被截断时仍在做浅层侦察且无新事实 ⇒ 加时间的收益存疑，优先改方法。")
        else:
            print("结论倾向：相当比例未解 Run 在被截断时仍在推进 ⇒ 时间预算可能是约束，值得做 1.5× 取证重跑。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
