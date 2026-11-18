"""P0 预注册实验（fa-causal-v1）的分组生成器 —— 只读。

实验目的：验证「首行动（First Action, FA）注入」是否**因果地**提高解出率。
预注册口径见 experiments/fa-causal-v1.yaml，本脚本只负责产出入组名单，
不启动任何 Run、不写数据库。

用法：
    python scripts/prepare_fa_causal.py --dry-run          # 用内置样本自测，不连库
    python scripts/prepare_fa_causal.py                    # 连库生成正式分组
    python scripts/prepare_fa_causal.py --ground <uuid>    # 指定练习场

分组规则（确定性、可复算）：
    1. 候选池 = 指定练习场中 status ∈ {UNSOLVED, PENDING} 的题
    2. 排除历史上已出现 exploit.first_action 的题（否则实验组与对照组同质化）
    3. 按 has_cve 分层（CVE 题与常规题的难度分布不同）
    4. 层内按 sha256(challenge_id) 升序，偶数位进 treatment，奇数位进 control

为什么不用随机数：预注册要求任何人在任何时间重跑都能得到同一份名单，
否则「事后选有利分组」无法被排除。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "experiments" / "fa-causal-v1.grouping.json"
DEFAULT_GROUND = "b9bbb32f-f186-458f-b90b-12440c0f6aea"  # 常规题组
TARGET_SIZE = 20

DB = {
    "host": "127.0.0.1",
    "port": 3307,
    "user": "ctf_agent",
    "password": "ctf_agent",
    "database": "ctf_agent",
    "charset": "utf8mb4",
}

CVE_RE = ("CVE-", "cve-")


def _hash_key(challenge_id: str) -> str:
    return hashlib.sha256(challenge_id.encode("utf-8")).hexdigest()


def has_cve(name: str) -> bool:
    return any(tag in name for tag in CVE_RE)


def assign_groups(candidates: list[dict], *, size: int = TARGET_SIZE) -> dict:
    """纯函数：候选池 -> 分组结果。

    candidates: [{challenge_id, name, run_count, fa_seen}]
    返回 {eligible, excluded_fa, selected, treatment, control, strata}
    """
    eligible = [c for c in candidates if not c.get("fa_seen")]
    excluded_fa = [c for c in candidates if c.get("fa_seen")]

    if len(eligible) < size:
        raise ValueError(
            f"可用候选只有 {len(eligible)} 道（需 {size} 道）；"
            f"已排除 {len(excluded_fa)} 道含 FA 历史的题"
        )

    strata: dict[str, list[dict]] = {"cve": [], "regular": []}
    for item in eligible:
        strata["cve" if item.get("has_cve") else "regular"].append(item)

    selected: list[dict] = []
    # 按层配额：CVE 层最多取一半，其余给常规题；层内不足则由另一层补足。
    quota_cve = min(len(strata["cve"]), size // 2)
    quota_regular = size - quota_cve
    if quota_regular > len(strata["regular"]):
        quota_cve = min(len(strata["cve"]), size - len(strata["regular"]))
        quota_regular = size - quota_cve

    for key, quota in (("cve", quota_cve), ("regular", quota_regular)):
        ordered = sorted(strata[key], key=lambda c: _hash_key(c["challenge_id"]))
        selected.extend(ordered[:quota])

    treatment = [c for i, c in enumerate(selected) if i % 2 == 0]
    control = [c for i, c in enumerate(selected) if i % 2 == 1]

    return {
        "eligible": eligible,
        "excluded_fa": excluded_fa,
        "selected": selected,
        "treatment": treatment,
        "control": control,
        "strata": {"cve": quota_cve, "regular": quota_regular},
    }


def _summary(grouping: dict, pool_size: int) -> dict:
    def stat(items: list[dict]) -> dict:
        runs = [c.get("run_count") or 0 for c in items]
        return {
            "n": len(items),
            "with_prior_runs": sum(1 for r in runs if r > 0),
            "median_prior_runs": sorted(runs)[len(runs) // 2] if runs else 0,
            "cve": sum(1 for c in items if c.get("has_cve")),
        }

    return {
        "pool_size": pool_size,
        "eligible_no_fa": len(grouping["eligible"]),
        "excluded_for_prior_fa": len(grouping["excluded_fa"]),
        "treatment": stat(grouping["treatment"]),
        "control": stat(grouping["control"]),
    }


def _fmt_rows(items: list[dict]) -> list[str]:
    out = ["| 组 | challenge_id | 题名 | CVE | 历史 Run |", "|---|---|---|---|---|"]
    for label, group in (("treatment", items["treatment"]), ("control", items["control"])):
        for c in group:
            cid = c["challenge_id"]
            out.append(
                f"| {label[:4]} | `{cid[:8]}` | {c['name'][:34]} | "
                f"{'Y' if c.get('has_cve') else '-'} | {c.get('run_count', 0)} |"
            )
    return out


def load_from_db(ground: str) -> list[dict]:
    import pymysql  # 延迟导入，--dry-run 不需要它

    conn = pymysql.connect(**DB, autocommit=True)  # autocommit 必开：否则快照冻结
    try:
        cur = conn.cursor()
        cur.execute(
            "select platform_challenge_id, challenge_name, status "
            "from arena_solve_queue where practice_ground_id=%s "
            "and status in ('UNSOLVED','PENDING') order by id",
            (ground,),
        )
        queue = cur.fetchall()
        names = [row[1] for row in queue]

        ids: dict[str, str] = {}
        for i in range(0, len(names), 20):
            batch = names[i:i + 20]
            placeholders = ",".join(["%s"] * len(batch))
            cur.execute(f"select id, name from challenges where name in ({placeholders})", batch)
            for cid, nm in cur.fetchall():
                ids[nm] = cid

        stats: dict[str, dict] = {}
        id_list = [ids[n] for n in names if n in ids]
        for i in range(0, len(id_list), 20):
            batch = id_list[i:i + 20]
            placeholders = ",".join(["%s"] * len(batch))
            cur.execute(
                "select r.challenge_id, count(distinct r.id) as runs, "
                "  max(case when e.event_type='exploit.first_action' then 1 else 0 end) as fa "
                "from solve_runs r left join run_events e on e.run_id=r.id "
                f"where r.challenge_id in ({placeholders}) group by r.challenge_id",
                batch,
            )
            for cid, runs, fa in cur.fetchall():
                stats[cid] = {"run_count": int(runs or 0), "fa_seen": bool(fa)}

        out = []
        for name in names:
            cid = ids.get(name)
            if not cid:
                continue  # 队列里有但 challenges 表无对应行：不参与实验
            row = stats.get(cid, {"run_count": 0, "fa_seen": False})
            out.append({
                "challenge_id": cid,
                "name": name,
                "run_count": row["run_count"],
                "fa_seen": row["fa_seen"],
                "has_cve": has_cve(name),
            })
        return out
    finally:
        conn.close()


def _dry_run_fixture() -> list[dict]:
    """内置 26 道合成候选，用于在无 DB 时验证分组逻辑。"""
    rows = []
    for i in range(26):
        cid = f"{i:08x}-0000-4000-8000-000000000000"
        rows.append({
            "challenge_id": cid,
            "name": (f"[演示]CVE-2019-{1000 + i} 样例" if i % 3 == 0 else f"[演示]常规题 {i}"),
            "run_count": i % 5,
            "fa_seen": i in (2, 7, 11),  # 三道已有 FA 历史，应被排除
            "has_cve": i % 3 == 0,
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 fa-causal-v1 的入组名单（只读）")
    ap.add_argument("--dry-run", action="store_true", help="用内置样本自测，不连库")
    ap.add_argument("--ground", default=DEFAULT_GROUND, help="练习场 UUID")
    ap.add_argument("--size", type=int, default=TARGET_SIZE, help="入组题数")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="输出 JSON 路径")
    args = ap.parse_args()

    candidates = _dry_run_fixture() if args.dry_run else load_from_db(args.ground)
    print(f"候选池: {len(candidates)} 道（ground={'(dry-run)' if args.dry_run else args.ground}）")

    try:
        grouping = assign_groups(candidates, size=args.size)
    except ValueError as exc:
        print(f"FAIL  {exc}")
        return 2

    summary = _summary(grouping, len(candidates))
    print(f"排除已有 FA 历史: {summary['excluded_for_prior_fa']} 道")
    print(f"可选(无 FA): {summary['eligible_no_fa']} 道 -> 入组 {args.size} 道"
          f"（CVE {grouping['strata']['cve']} / 常规 {grouping['strata']['regular']}）")
    for label in ("treatment", "control"):
        s = summary[label]
        print(f"  {label:<10} n={s['n']:>2}  CVE={s['cve']:>2}  "
              f"有历史Run={s['with_prior_runs']:>2}  历史Run中位={s['median_prior_runs']}")

    payload = {
        "experiment": "fa-causal-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "practice_ground_id": None if args.dry_run else args.ground,
        "dry_run": bool(args.dry_run),
        "rule": "strata=has_cve; order=sha256(challenge_id); even->treatment, odd->control",
        "summary": summary,
        "treatment": grouping["treatment"],
        "control": grouping["control"],
        "excluded_for_prior_fa": [c["challenge_id"] for c in grouping["excluded_fa"]],
        "markdown_table": _fmt_rows(grouping),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        shown = args.out.resolve().relative_to(ROOT)
    except ValueError:
        shown = args.out
    print(f"已写入 {shown}")

    if args.dry_run:
        assert len(grouping["treatment"]) + len(grouping["control"]) == args.size
        assert all(not c["fa_seen"] for c in grouping["selected"])
        assert abs(len(grouping["treatment"]) - len(grouping["control"])) <= 1
        print("自检通过：入组总数正确、无 FA 历史混入、两组数量平衡")
    return 0


if __name__ == "__main__":
    sys.exit(main())
