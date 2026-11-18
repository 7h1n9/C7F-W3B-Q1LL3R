"""离线评测：知识库按「观测信号」索引，是否比按「题名」索引更可迁移。

背景
----
2026-10-29 的夜间轮次里，arena 标题三轴（box_names / CVE / product+technique）
命中 0/11；从历史记录蒸馏出 147 条语料后同题命中 9/11 —— 但那是同题复用，
真实赛场不会出现同一道题。所以「题名索引」的天花板是零，必须验证另一条路。

本脚本把每条 learned 记录拆成两条互相独立的轴，然后只允许用 A 轴检索：

  A 观测轴  启动后立刻能看到的东西：Server/X-Powered-By 头、框架名与版本、
            扩展名、URL 路径、参数名、Cookie 名。与题名无关。
  B 技法轴  这条记录实际打的是什么：sqli / lfi / ssti / deser / upload / rce ...

评测问题：遮住题名，只用 A 轴，能否检索到 B 轴相同的邻居。
答「能」，signal 索引在赛场上就有意义；答「不能」，说明蒸馏产物只剩答案值，
需要改蒸馏源（run_events 原始记录），而不是继续扩题名语料。

对照基线：
  title     现状机制 —— 用题名 token 做同样的检索
  random    随机邻居，用来确认指标不是恒 100%

用法：
  .venv3.11/Scripts/python.exe scripts/eval_signal_index.py
  .venv3.11/Scripts/python.exe scripts/eval_signal_index.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "data" / "knowledge" / "playbooks" / "learned-boxes.jsonl"
LABELS = ROOT / "scripts" / "signal_index_labels.json"

# --- A 轴：观测信号 ------------------------------------------------------
# 这些词在别的题上照样出现，因为它们描述的是「靶机长什么样」而不是「这题叫什么」。

STACKS = {
    "tomcat": "tomcat", "nginx": "nginx", "apache": "apache", "iis": "iis",
    "flask": "flask", "werkzeug": "werkzeug", "django": "django",
    "express": "express", "koa": "koa", "node": "node", "nodejs": "node",
    "spring": "spring", "thinkphp": "thinkphp", "laravel": "laravel",
    "struts": "struts", "shiro": "shiro", "php": "php", "java": "java",
    "python": "python", "golang": "go", "jquery": "jquery",
    "mysql": "mysql", "sqlite": "sqlite", "redis": "redis", "mongodb": "mongodb",
    "fastapi": "fastapi", "uvicorn": "uvicorn", "gunicorn": "gunicorn",
}

HEADERS = ("server", "x-powered-by", "set-cookie", "x-aspnet-version",
           "x-generator", "via", "x-frame-options")

EXTS = ("php", "jsp", "jspx", "do", "action", "asp", "aspx", "cgi", "py", "js", "json")

# --- B 轴：技法标签 ------------------------------------------------------
# 检索时禁止使用，只作为「正确答案」参与打分，用来保证无泄漏。

TECHNIQUES: dict[str, tuple[str, ...]] = {
    "sqli": ("sql injection", "sqli", "union select", "information_schema",
             "extractvalue", "updatexml", "盲注", "报错注入", "注入点", "sql 注入"),
    "lfi": ("lfi", "文件包含", "本地包含", "php://filter", "php://input",
            "/etc/passwd", "目录穿越", "路径穿越", "任意文件读"),
    "rce": ("rce", "命令执行", "代码执行", "远程命令", "system(", "exec(",
            "passthru", "shell_exec", "反弹 shell", "getshell"),
    "ssti": ("ssti", "模板注入", "服务端模板", "jinja", "twig", "freemarker",
             "velocity", "{{7*7}}", "{{7*'7'}}"),
    "deser": ("反序列化", "unserialize", "deserial", "pickle", "ysoserial",
              "gadget", "pop chain", "__wakeup", "__destruct"),
    "upload": ("文件上传", "上传绕过", "webshell", ".phar", ".htaccess",
               "content-type 绕过", "upload"),
    "ssrf": ("ssrf", "gopher", "内网探测", "服务端请求伪造", "dict://"),
    "xxe": ("xxe", "实体注入", "外部实体", "doctype", "xml 注入"),
    "jwt": ("jwt", "alg=none", "弱密钥", "json web token", "签名校验"),
    "auth": ("越权", "弱口令", "逻辑漏洞", "绕过登录", "idor", "垂直越权",
             "水平越权", "验证码绕过", "爆破", "密码重置"),
    "info": ("源码泄露", "源码泄漏", "备份文件", "www.zip", "robots.txt",
             "目录扫描", "目录遍历", ".git", ".svn", "swp", "ds_store"),
    "xss": ("xss", "跨站脚本", "alert(", "onerror="),
    "php_trick": ("弱比较", "md5 碰撞", "type juggling", "== 绕过", "0e",
                  "preg_replace /e", "变量覆盖", "extract("),
}

# 答案值：出现在记录里但没有迁移价值，一律丢弃，否则检索会被 flag/哈希带偏。
NOISE_RE = re.compile(
    r"""(?xi)
    \b[0-9a-f]{16,}\b            # 长 hex（哈希、md5、token）
    | [0-9a-f]{8}-[0-9a-f]{4}-   # uuid
    | \b(?:flag|ctf2?)\{[^}]*\}  # flag 字面量
    | \b\d{6,}\b                 # 长数字（时间戳、端口高位）
    """
)

WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{1,40}|[\u4e00-\u9fff]{2,8}")
PATH_RE = re.compile(r"(/[A-Za-z0-9_\-./]{1,60})")
PARAM_RE = re.compile(r"[?&]([A-Za-z_][A-Za-z0-9_]{0,30})=")
COOKIE_RE = re.compile(r"(?:set-cookie:|cookie:)\s*([A-Za-z_][A-Za-z0-9_]{1,24})", re.I)


def _strip_noise(text: str) -> str:
    return NOISE_RE.sub(" ", text)


def observation_signals(text: str) -> set[str]:
    """A 轴：从一段记录里抽出「靶机长什么样」的信号。不含技法词。"""
    text = _strip_noise(text)
    low = text.lower()
    out: set[str] = set()

    for word, canon in STACKS.items():
        if re.search(rf"\b{re.escape(word)}\b", low):
            out.add(f"stack:{canon}")

    for hdr in HEADERS:
        if hdr in low:
            out.add(f"hdr:{hdr}")

    for ext in EXTS:
        if re.search(rf"\.{ext}\b", low):
            out.add(f"ext:{ext}")

    for m in PARAM_RE.finditer(text):
        out.add(f"param:{m.group(1).lower()}")

    for m in COOKIE_RE.finditer(text):
        out.add(f"cookie:{m.group(1).lower()}")

    for m in PATH_RE.finditer(text):
        seg = m.group(1).rstrip(".").lower()
        # 只保留有语义的路径段，避免 / 和 /1 这类噪声
        if len(seg) >= 4 and not seg.strip("/").isdigit():
            out.add(f"path:{seg}")

    return out


def technique_labels(text: str) -> set[str]:
    """B 轴：这条记录实际打的是什么。只用于判定命中，不参与检索。"""
    low = text.lower()
    out: set[str] = set()
    for name, needles in TECHNIQUES.items():
        for needle in needles:
            if needle in low:
                out.add(name)
                break
    return out


# 取「主技法」时的优先级：越具体的越靠前，``rce`` / ``auth`` 这类泛类垫底。
# 一条记录只判一个标签，否则「交集非空」会被 rce 这种占 60% 的宽类吃满，
# 命中率虚高到随机基线也有一半。
PRIMARY_ORDER = ("deser", "ssti", "xxe", "ssrf", "jwt", "lfi", "upload",
                 "sqli", "php_trick", "xss", "info", "auth", "rce")


def primary_technique(text: str) -> str:
    labels = technique_labels(text)
    for name in PRIMARY_ORDER:
        if name in labels:
            return name
    return ""


def title_tokens(title: str) -> set[str]:
    """现状机制的检索键：题名里 ≥3 字符的英文词与中文片段。"""
    toks = {t.lower() for t in WORD_RE.findall(title)}
    return {f"t:{t}" for t in toks if len(t) >= 3}


def _idf(docs: list[set[str]]) -> dict[str, float]:
    df: Counter[str] = Counter()
    for d in docs:
        df.update(d)
    n = max(len(docs), 1)
    return {t: math.log((n + 1) / (c + 0.5)) for t, c in df.items()}


def _score(query: set[str], cand: set[str], idf: dict[str, float]) -> float:
    if not query or not cand:
        return 0.0
    shared = query & cand
    if not shared:
        return 0.0
    num = sum(idf.get(t, 1.0) for t in shared)
    den = math.sqrt(sum(idf.get(t, 1.0) for t in query) *
                    sum(idf.get(t, 1.0) for t in cand))
    return num / den if den else 0.0


def _rank(query: set[str], docs: list[set[str]], idf: dict[str, float],
          skip: int) -> list[int]:
    scored = [(i, _score(query, d, idf)) for i, d in enumerate(docs) if i != skip]
    scored.sort(key=lambda p: -p[1])
    return [i for i, s in scored if s > 0]


def evaluate(records: list[dict], rng: random.Random,
             labels: dict[str, str] | None = None) -> dict:
    """Score the observation index against a technique label.

    ``labels`` is the human ground truth (``signal_index_labels.json``).  It
    matters: the regex labeler that used to drive this scored **41%** on a
    34-entry sample -- it called ``easy_tornado`` (SSTI) "unknown",
    ``Fakebook`` (SSRF) "lfi" and ``HardSQL`` (SQLi) "info".  A retrieval
    metric built on a label that is wrong half the time measures nothing, so
    the regex is now only a fallback and its agreement is reported.
    """
    n = len(records)
    texts = []
    for r in records:
        parts = [str(x) for x in (r.get("steps") or [])]
        parts += [str(x) for x in (r.get("first_action") or "").split("|")]
        parts += [str(x) for x in (r.get("known_failures") or [])]
        texts.append(" ".join(parts))

    obs = [observation_signals(t) for t in texts]
    regex_tech = [primary_technique(t) for t in texts]
    names = [title_tokens(str(r.get("title") or "")) for r in records]

    agreement: float | None = None
    if labels:
        tech = []
        for r in records:
            lab = str(labels.get(str(r.get("title") or "").strip()) or "").lower()
            tech.append("" if lab in ("", "unknown") else lab)
        pairs = [(a, b) for a, b in zip(regex_tech, tech) if b]
        if pairs:
            agreement = sum(1 for a, b in pairs if a == b) / len(pairs)
    else:
        tech = regex_tech

    obs_idf = _idf(obs)
    name_idf = _idf(names)

    # 只评测「主技法可判定」的题；抽不出技法的题无法判定命中，单独报数。
    evaluable = [i for i in range(n) if tech[i]]
    skipped_no_tech = [i for i in range(n) if not tech[i]]
    if not evaluable:
        return {"error": "no evaluable records"}

    counts = Counter(tech[i] for i in evaluable)
    majority_label, majority_n = counts.most_common(1)[0]

    stats = {k: {"n": 0, "obs1": 0, "obs3": 0, "name1": 0, "name3": 0}
             for k in counts}
    obs_empty = name_empty = 0
    detail: list[dict] = []

    for i in evaluable:
        got_obs = _rank(obs[i], obs, obs_idf, i)
        got_name = _rank(names[i], names, name_idf, i)
        if not got_obs:
            obs_empty += 1
        if not got_name:
            name_empty += 1

        def hit(ranked: list[int], k: int) -> bool:
            return tech[i] in [tech[j] for j in ranked[:k]]

        s = stats[tech[i]]
        s["n"] += 1
        s["obs1"] += int(hit(got_obs, 1))
        s["obs3"] += int(hit(got_obs, 3))
        s["name1"] += int(hit(got_name, 1))
        s["name3"] += int(hit(got_name, 3))

        if len(detail) < 12:
            detail.append({
                "title": str(records[i].get("title") or ""),
                "tech": tech[i],
                "n_signals": len(obs[i]),
                "signals": sorted(obs[i])[:6],
                "obs_top1": (str(records[got_obs[0]].get("title") or "")
                             if got_obs else None),
                "obs_top1_tech": tech[got_obs[0]] if got_obs else None,
                "obs_top1_hit": bool(got_obs and hit(got_obs, 1)),
            })

    m = len(evaluable)
    sizes = sorted(len(o) for o in obs)

    def total(key: str, field: str) -> float:
        return sum(v[field] for v in stats.values()) / m

    # macro：按技法类别平均，避免 rce/info 这类大类的样本量把整体拉平。
    macro_classes = [k for k, v in stats.items() if v["n"] >= 2]
    macro = {
        f: (sum(stats[k][f] / stats[k]["n"] for k in macro_classes) /
            max(len(macro_classes), 1))
        for f in ("obs1", "obs3", "name1", "name3")
    }

    return {
        "records": n,
        "evaluable": m,
        "labels_source": "manual" if labels else "regex",
        "label_agreement": agreement,
        "no_technique_label": len(skipped_no_tech),
        "obs_signal_density": {
            "min": sizes[0] if sizes else 0,
            "median": sizes[len(sizes) // 2] if sizes else 0,
            "max": sizes[-1] if sizes else 0,
            "zero": sum(1 for s in sizes if s == 0),
        },
        "technique_distribution": counts.most_common(),
        "obs_index": {"hit@1": total("", "obs1"), "hit@3": total("", "obs3"),
                      "empty_query_rate": obs_empty / m},
        "title_index": {"hit@1": total("", "name1"), "hit@3": total("", "name3"),
                        "empty_query_rate": name_empty / m},
        "majority_baseline": {"label": majority_label,
                              "hit@1": majority_n / m, "hit@3": majority_n / m},
        "macro": macro,
        "macro_classes": len(macro_classes),
        "per_class": {k: v for k, v in
                      sorted(stats.items(), key=lambda p: -p[1]["n"])},
        "detail": detail,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=str(CORPUS))
    ap.add_argument("--labels", default=str(LABELS),
                    help="human ground truth; pass '' to fall back to the regex labeler")
    ap.add_argument("--json", default="")
    ap.add_argument("--seed", type=int, default=20261029)
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    path = Path(args.corpus)
    if not path.exists():
        print(f"corpus not found: {path}")
        return 2
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
               if line.strip()]
    records = [r for r in records if r.get("kind") == "poc"]

    labels: dict[str, str] | None = None
    if args.labels:
        label_path = Path(args.labels)
        if label_path.exists():
            raw = json.loads(label_path.read_text(encoding="utf-8"))
            labels = {k: v for k, v in raw.items() if not k.startswith("_")}
        else:
            print(f"labels not found ({label_path}); falling back to the regex labeler",
                  file=sys.stderr)

    report = evaluate(records, random.Random(args.seed), labels)

    def pct(x: float) -> str:
        return f"{x * 100:5.1f}%"

    print("=" * 66)
    print("知识库索引迁移性评测（leave-one-box-out，题名全部遮住）")
    print("=" * 66)
    print(f"语料            {report['records']} 条 poc")
    desc = "人工标注" if report["labels_source"] == "manual" else "正则抽取"
    print(f"可评测          {report['evaluable']} 条（技法标签 = {desc}）")
    print(f"标签不可判定    {report['no_technique_label']} 条")
    if report["label_agreement"] is not None:
        print(f"正则 vs 人工   一致率 {report['label_agreement'] * 100:.1f}% "
              f"（正则标签不能用来下结论）")
    d = report["obs_signal_density"]
    print(f"观测信号/条     min {d['min']}  中位 {d['median']}  max {d['max']}  "
          f"空 {d['zero']}")
    print()
    print("技法分布 " + ", ".join(f"{k}:{v}"
                                  for k, v in report["technique_distribution"]))
    print()
    print(f"{'检索键':<14}{'hit@1':>8}{'hit@3':>8}{'无结果率':>10}{'macro hit@3':>13}")
    print("-" * 53)
    o, t = report["obs_index"], report["title_index"]
    mac, maj = report["macro"], report["majority_baseline"]
    print(f"{'观测信号 A':<14}{pct(o['hit@1']):>8}{pct(o['hit@3']):>8}"
          f"{pct(o['empty_query_rate']):>10}{pct(mac['obs3']):>13}")
    print(f"{'题名 token':<14}{pct(t['hit@1']):>8}{pct(t['hit@3']):>8}"
          f"{pct(t['empty_query_rate']):>10}{pct(mac['name3']):>13}")
    print(f"{'多数类基线':<14}{pct(maj['hit@1']):>8}{pct(maj['hit@3']):>8}"
          f"{'-':>10}{pct(maj['hit@3']):>13}   (全猜 {maj['label']})")
    print(f"（macro 按 {report['macro_classes']} 个 ≥2 样本的技法类别平均）")
    print()

    print("分技法明细：")
    print(f"  {'技法':<11}{'n':>4}{'obs@1':>8}{'obs@3':>8}{'name@1':>9}{'name@3':>9}")
    for k, v in report["per_class"].items():
        nn = max(v["n"], 1)
        print(f"  {k:<11}{v['n']:>4}{pct(v['obs1'] / nn):>8}{pct(v['obs3'] / nn):>8}"
              f"{pct(v['name1'] / nn):>9}{pct(v['name3'] / nn):>9}")
    print()

    gain = (mac["obs3"] - mac["name3"]) * 100
    over_major = (mac["obs3"] - maj["hit@3"]) * 100
    print(f"观测信号 vs 题名     macro hit@3 增益 {gain:+.1f} 个百分点")
    print(f"观测信号 vs 多数类   macro hit@3 增益 {over_major:+.1f} 个百分点")
    print()
    if over_major <= 5:
        print("结论：观测信号打不过「全猜最常见技法」——语料里几乎没有可迁移判据，"
              "换索引键救不了，必须改蒸馏源（run_events 的原始命令与响应）。")
    elif gain <= 5:
        print("结论：观测信号与题名索引差不多 —— 换键不带来收益，"
              "瓶颈在语料质量而不是索引键。")
    else:
        print("结论：遮住题名后观测信号仍能定位同类技法，且显著优于题名与多数类，"
              "signal 索引方向成立；下一步应扩大观测面（一手事件），而不是扩题名。")

    print()
    print("样本明细（前 10 条）：")
    for row in report["detail"][:10]:
        mark = "OK  " if row["obs_top1_hit"] else "MISS"
        print(f"  [{mark}] {row['title'][:26]} | 期望 {row['tech']} | "
              f"取回 {row['obs_top1_tech'] or '-'} | 信号 {row['n_signals']} | "
              f"{('/'.join(row['signals'][:3]))[:52]}")

    if args.json:
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n报告已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
