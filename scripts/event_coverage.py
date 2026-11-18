#!/usr/bin/env python
"""回答一个问题：某个功能是否产出了 0 个事件。

「0 条事件」是本项目最贵的错：代码写了、测试过了、没人调用，然后基于 0 条数据
讨论效果。本 CLI 把 ``metrics_guard.coverage_report`` 接到 ``run_events`` 上，
按 event_type 统计计数，实际为 0 的显式标 ``ZERO``。

与 ``assert_feature_live`` 的分工：

* ``assert_feature_live``：代码 mtime vs 最后事件时间 → 判断「当前这版代码跑过没」
* ``event_coverage``：事件类型 vs 期望产出 → 判断「这个埋点整体有没有产出」

失败语义（重要）：读不到数据库时**不打印 0**。0 条是证据，连不上是没证据，
两者混同就是「断库 → 0% → 功能无效」的误判路径。

事件名一律带 ``muteki.`` 前缀（``app/solver/muteki/adapter/event_bridge.py``
统一加），写成 ``exploit.first_action`` 会永远查到 0 条。

用法：
    python scripts/event_coverage.py
    python scripts/event_coverage.py --expect muteki.worker_step=1000
    python scripts/event_coverage.py --expect muteki.exploit.first_action
    python scripts/event_coverage.py --liveness backend/app/solver/muteki/coordinator.py=muteki.exploit.first_action
    python scripts/event_coverage.py --db-port 3399   # 演示无数据源分支

退出码：0=全部达标 1=有 0 事件或低于期望 2=无数据源（无法判定） 3=用法错误
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.metrics_guard import (  # noqa: E402  (path set up above)
    assert_feature_live,
    coverage_report,
)

EXIT_OK = 0
EXIT_MISSING = 1
EXIT_NO_SOURCE = 2
EXIT_USAGE = 3

#: 当前关心的四类事件。改动这里等于改动「这个看板在盯什么」，请同时写进 docs。
DEFAULT_EXPECTED = (
    "muteki.exploit.first_action",
    "muteki.fact_added",
    "muteki.worker_step",
    "muteki.worker_command",
)

COUNT_SQL = (
    "SELECT event_type, COUNT(*) FROM run_events "
    "WHERE event_type IN ({placeholders}) GROUP BY event_type"
)
LAST_EVENT_SQL = (
    "SELECT MAX(created_at) AS last_event_at, COUNT(*) AS n "
    "FROM run_events WHERE event_type = %s"
)


def _parse_expect(raw: list[str]) -> dict[str, int | None]:
    """``event_type=N`` → 精确期望；``event_type`` → 期望 >0（None）。"""

    out: dict[str, int | None] = {}
    for item in raw:
        name, sep, count = item.partition("=")
        name = name.strip()
        if not name:
            raise ValueError(f"--expect 缺少事件类型: {item!r}")
        if name in out:
            raise ValueError(f"--expect 重复声明: {name}")
        if not sep:
            out[name] = None
            continue
        try:
            out[name] = int(count)
        except ValueError as exc:
            raise ValueError(f"--expect 的计数不是整数: {item!r}") from exc
        if out[name] < 0:
            raise ValueError(f"--expect 的计数不能为负: {item!r}")
    return out


def _parse_liveness(raw: list[str]) -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    for item in raw:
        code, sep, event = item.rpartition("=")
        if not sep or not code.strip() or not event.strip():
            raise ValueError(f"--liveness 需要 CODE_PATH=EVENT_TYPE，收到 {item!r}")
        path = Path(code.strip())
        if not path.is_absolute():
            path = ROOT / path
        out.append((path, event.strip()))
    return out


def _connect(args: argparse.Namespace):
    import pymysql

    return pymysql.connect(
        host=args.db_host,
        port=args.db_port,
        user=args.db_user,
        password=args.db_password,
        database=args.db_name,
        charset="utf8mb4",
        connect_timeout=args.db_timeout,
        read_timeout=args.db_timeout,
        write_timeout=args.db_timeout,
        autocommit=True,
    )


def fetch_counts(args: argparse.Namespace, event_types: list[str]):
    """返回 ``(counts, source)``；任何读不到数据的情况返回 ``(None, 原因)``。"""

    source = (
        f"mysql://{args.db_user}@{args.db_host}:{args.db_port}/{args.db_name}"
        f" :: {COUNT_SQL.format(placeholders='...')}"
    )
    try:
        connection = _connect(args)
    except ImportError as exc:  # pragma: no cover - venv always has it
        return None, f"{source}（pymysql 不可用: {exc}）"
    except Exception as exc:  # noqa: BLE001 - 连接失败要报出来，不是吞掉
        return None, f"{source}（连接失败: {type(exc).__name__}: {exc}）"

    try:
        with connection.cursor() as cursor:
            placeholders = ",".join(["%s"] * len(event_types))
            cursor.execute(COUNT_SQL.format(placeholders=placeholders), event_types)
            counts = {str(name): int(n or 0) for name, n in cursor.fetchall()}
    except Exception as exc:  # noqa: BLE001
        return None, f"{source}（查询失败: {type(exc).__name__}: {exc}）"
    finally:
        connection.close()
    return counts, source


def fetch_last_event(args: argparse.Namespace, event_type: str):
    connection = _connect(args)
    try:
        with connection.cursor() as cursor:
            cursor.execute(LAST_EVENT_SQL, (event_type,))
            row = cursor.fetchone() or (None, 0)
    finally:
        connection.close()
    count = int(row[1] or 0)
    last_at: datetime | None = row[0]
    if count == 0 or last_at is None:
        return [], f"mysql run_events[{event_type}]（0 条）"
    # 只取 MAX(created_at) 参与 mtime 比对，避免把几万行时间戳全读进内存；
    # 因此判定里的「共 1 条」指送入判定的样本，不是表里的真实条数。
    return [last_at], (
        f"mysql run_events[{event_type}]（表内共 {count} 条，仅取 MAX(created_at) 参与比对）"
    )


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(
        description="按 event_type 统计 run_events 覆盖率；0 事件显式标出，无数据源不报 0。",
        epilog="退出码: 0=达标 1=有 0 事件/低于期望 2=无数据源(无法判定) 3=用法错误",
    )
    ap.add_argument(
        "--expect",
        action="append",
        default=[],
        metavar="EVENT_TYPE[=COUNT]",
        help=f"期望值，可多次传入；省略 COUNT 表示期望 >0。默认检查: {', '.join(DEFAULT_EXPECTED)}",
    )
    ap.add_argument(
        "--liveness",
        action="append",
        default=[],
        metavar="CODE_PATH=EVENT_TYPE",
        help="附带做 assert_feature_live：当前代码是否真的产出过该事件（可多次）",
    )
    ap.add_argument("--db-host", default="127.0.0.1")
    ap.add_argument("--db-port", type=int, default=3307)
    ap.add_argument("--db-user", default="ctf_agent")
    ap.add_argument("--db-password", default="ctf_agent")
    ap.add_argument("--db-name", default="ctf_agent")
    ap.add_argument("--db-timeout", type=int, default=3, help="连接/读写超时秒数，默认 3")
    args = ap.parse_args(argv)

    try:
        expected = _parse_expect(args.expect) if args.expect else {
            name: None for name in DEFAULT_EXPECTED
        }
        liveness = _parse_liveness(args.liveness)
    except ValueError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return EXIT_USAGE

    for code_path, _ in liveness:
        if not code_path.is_file():
            print(f"[错误] --liveness 的代码文件不存在: {code_path}", file=sys.stderr)
            print("       路径写错会让 mtime 比对变成对空文件的比对", file=sys.stderr)
            return EXIT_USAGE

    counts, source = fetch_counts(args, list(expected))
    if counts is None:
        print("[事件覆盖率] 无法判定（无数据源）")
        print(f"  数据源: {source}")
        print("  结论: 没有读到 run_events，既不能确认有事件，也不能断言 0 事件。")
        print("  这不是『0 条』：0 条是证据，连不上是没证据。请先恢复 DB 3307 或导出 dump。")
        return EXIT_NO_SOURCE

    report = coverage_report(expected, counts)
    print(report)
    print(f"\n  数据源: {source}")
    print(f"  查询时间: {datetime.now().astimezone().isoformat(timespec='seconds')}")

    # 声明了但表里根本不存在的类型，与「存在但为 0」不同：前者是埋点名写错。
    unknown = sorted(set(expected) - set(counts))
    if unknown:
        print(f"  表中未出现（不是 0，是没这个 event_type）: {', '.join(unknown)}")
        print("  先确认事件名带 muteki. 前缀，再怀疑埋点缺失。")

    exit_code = EXIT_OK if report.ok else EXIT_MISSING

    if liveness:
        print("\n[功能存活检查] 代码 mtime vs 最后事件时间")
        for code_path, event_type in liveness:
            try:
                times, live_source = fetch_last_event(args, event_type)
            except Exception as exc:  # noqa: BLE001
                times, live_source = None, f"mysql（查询失败: {type(exc).__name__}: {exc}）"
            verdict = assert_feature_live(
                event_type,
                event_times=times,
                code_path=code_path,
                data_source=live_source,
                strict=False,
            )
            print(verdict.explain())
            print("")
            if not verdict.ok:
                exit_code = max(exit_code, EXIT_MISSING) if verdict.status == "NEVER_RAN" else EXIT_NO_SOURCE
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
