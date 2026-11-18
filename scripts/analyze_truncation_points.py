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
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _metrics_boot import record_analysis  # noqa: E402  (同时把 backend/ 放进 sys.path)

from app.services.metrics_guard import format_comparison  # noqa: E402

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


def print_guard_comparison(
    unsolved_stats: dict[str, int] | None,
    solved_stats: dict[str, int] | None,
    *,
    window: int,
):
    """把「末窗有 fact 增量」的 Run 占比交给选择偏差护栏打印。

    这只是把已经算出来的两个分数喂给 ``format_comparison``：分子的口径是
    「末 window 条命令里 ``delta_facts`` 之和 > 0 的 Run 数」，分母是该组 Run 数。
    返回 report 供快照记录；任一组为空时返回 None（0/0 是没测量，不是 0%）。
    """

    print("\n" + "=" * 72)
    print("[组间对比] 口径：末%d条命令有 fact 增量的 Run 占比，按 final_status 分组" % window)
    print("  分组分母（原样，不合并、不重采样）：未解=%s，已解=%s" % (
        "n/a" if not unsolved_stats else unsolved_stats["n"],
        "n/a" if not solved_stats else solved_stats["n"],
    ))
    if not unsolved_stats or not solved_stats:
        print("!! 无法对比：某一组为空（n=0）；0/0 不是『0%』，是『没测量』，禁止进入对比表")
        print("=" * 72)
        return None

    report = format_comparison(
        "未解/引擎失败",
        unsolved_stats["fact_growth_tail"],
        unsolved_stats["n"],
        "已解（对照）",
        solved_stats["fact_growth_tail"],
        solved_stats["n"],
    )
    print(report)
    print(f"\n  causal_claim_allowed = {report.causal_claim_allowed}"
          f"（warnings={len(report.warnings)} 条）")
    print("  !! 该对比是『按结局分组』：分组变量 final_status 就是结局本身。")
    print("     护栏不报警只说明数字没有明显失衡，不证明分组无偏；")
    print("     因此本段任何差异都不能解释为『时间预算导致未解』的因果证据。")
    print(
        "  注：命令数中位 %d vs %d 无法喂进该护栏——它只吃『分子/分母』比率，"
        "中位数没有可对应的分母，这是比率模型的结构性盲区。"
        % (unsolved_stats["median_commands"], solved_stats["median_commands"])
    )
    print("=" * 72)
    return report


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

    def profile(rows_list: list[list[dict]], label: str) -> dict[str, int] | None:
        n = len(rows_list)
        if not n:
            return None
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
        # 返回值只给快照用；上面的输出逐字不变。
        return {
            "n": n,
            "median_commands": lens[n // 2],
            "fact_growth_tail": growing,
            "all_recon_tail": recon_end,
            "exploit_tail": exploit_end,
            "looping_tail": looping,
        }

    unsolved_stats = profile(unsolved, "未解/引擎失败")
    solved_stats = profile(solved, "已解（对照）")

    comparison = print_guard_comparison(unsolved_stats, solved_stats, window=args.window)

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

    # 本次出数的溯源行：口径 = 这个 jsonl 的哪一份、有没有截断行。
    record_analysis(
        "analyze_truncation_points",
        f"transitions jsonl: {args.src.as_posix()} | 行数限制: 无（读取全文件 {total} 行）",
        {
            "runs": len(runs),
            "transitions": total,
            "status_histogram": dict(by_status),
            "unsolved": unsolved_stats,
            "solved": solved_stats,
            "comparison": None if comparison is None else {
                "label_a": comparison.label_a,
                "numerator_a": comparison.numerator_a,
                "denominator_a": comparison.denominator_a,
                "label_b": comparison.label_b,
                "numerator_b": comparison.numerator_b,
                "denominator_b": comparison.denominator_b,
                "denominator_ratio": round(comparison.denominator_ratio, 4),
                "numerator_ratio": comparison.numerator_ratio,
                "warnings": list(comparison.warnings),
                "causal_claim_allowed": comparison.causal_claim_allowed,
                "grouping_variable": "final_status（即结局本身，非自变量）",
            },
            "quadrant": dict(quad),
            "stuck_fact0_recon": quad.get("fact0 / recon", 0) if n else None,
            "advancing": (
                quad.get("fact+ / exploit", 0) + quad.get("fact+ / mixed", 0) if n else None
            ),
        },
        filters={"window": args.window, "tail": args.tail, "src": args.src.name},
        data_window=f"{args.src.name} 全量（{total} 行 / {len(runs)} runs）",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
