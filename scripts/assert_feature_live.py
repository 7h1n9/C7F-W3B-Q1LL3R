#!/usr/bin/env python
"""Refuse to report a feature's numbers when the feature never ran.

This is a shell around ``app.services.metrics_guard``: the decision lives in the
module, the shell only does I/O (read event timestamps, read mtime, print,
append a snapshot).  Kept separate so the rules can be unit-tested without a
database and without a clock.

Exit codes are part of the contract:

    0  LIVE       last event is newer than the code; the feature did run
    1  NEVER_RAN  code is newer than the last event; do not report numbers
    2  UNKNOWN    no data source (DB down, no dump); do not read as a pass
    3  usage      bad arguments or a missing code file

Examples
--------
    python scripts/assert_feature_live.py \\
        --event-type exploit.first_action \\
        --code backend/app/solver/muteki/coordinator.py

    python scripts/assert_feature_live.py \\
        --event-type exploit.first_action \\
        --code backend/app/solver/muteki/coordinator.py \\
        --from-json events.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.services.metrics_guard import (  # noqa: E402  (path set up above)
    DEFAULT_SNAPSHOT_LOG,
    FeatureNeverRan,
    MetricsDataUnavailable,
    assert_feature_live,
    file_mtime_iso,
    snapshot,
)

EXIT_LIVE = 0
EXIT_NEVER_RAN = 1
EXIT_UNKNOWN = 2
EXIT_USAGE = 3

DB_SQL = (
    "SELECT MAX(created_at) AS last_event_at, COUNT(*) AS n "
    "FROM run_events WHERE event_type = %s"
)


def _parse_times(raw: Any) -> list[datetime] | None:
    """Accept ``["2026-11-18T05:00:00Z", ...]`` or ``{"event_times": [...]}``."""

    if isinstance(raw, dict):
        if "event_times" not in raw:
            raise ValueError("JSON 对象缺少 event_times 字段")
        raw = raw["event_times"]
    if not isinstance(raw, list):
        raise ValueError("JSON 必须是时间字符串数组，或含 event_times 数组的对象")
    return [datetime.fromisoformat(str(item).replace("Z", "+00:00")) for item in raw]


def _load_from_json(path: Path) -> tuple[list[datetime] | None, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return _parse_times(payload), f"json:{path.as_posix()}"


def _load_from_mysql(args: argparse.Namespace, event_type: str) -> tuple[list[datetime] | None, str]:
    """Read event timestamps; any failure returns ``None`` (UNKNOWN), never ``[]``.

    The distinction matters: an empty list means "the table says zero events"
    and is evidence the feature never ran, while a failed connection is no
    evidence at all.  Conflating them is how "no data" becomes "0%".
    """

    source = (
        f"mysql://{args.db_user}@{args.db_host}:{args.db_port}/{args.db_name}"
        f" :: {DB_SQL} [{event_type}]"
    )
    try:
        import pymysql
    except ImportError as exc:  # pragma: no cover - venv always has it
        return None, f"{source}（pymysql 不可用: {exc}）"

    try:
        connection = pymysql.connect(
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
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        return None, f"{source}（连接失败: {type(exc).__name__}: {exc}）"

    try:
        with connection.cursor() as cursor:
            cursor.execute(DB_SQL, (event_type,))
            row = cursor.fetchone()
    except Exception as exc:  # noqa: BLE001
        return None, f"{source}（查询失败: {type(exc).__name__}: {exc}）"
    finally:
        connection.close()

    count = int((row or (None, 0))[1] or 0)
    last_at = (row or (None, None))[0]
    if count == 0 or last_at is None:
        return [], source
    return [last_at], source


def _record_snapshot(
    args: argparse.Namespace,
    *,
    source: str,
    verdict: Any,
    code_mtime: str,
) -> None:
    if not args.snapshot_log:
        return
    record = snapshot(
        sql_or_source=source,
        filters={"event_type": args.event_type},
        code_mtime=code_mtime,
        data_window="run_events 全表（无时间过滤）",
        result={
            "status": verdict.status,
            "ok": verdict.ok,
            "event_count": verdict.event_count,
            "last_event_at": verdict.last_event_at.isoformat() if verdict.last_event_at else None,
            "lag_hours": verdict.lag_hours,
        },
        path=ROOT / args.snapshot_log,
    )
    print(f"[snapshot] {record.query_id} -> {args.snapshot_log}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="assert_feature_live.py",
        description="检查某事件类型是否真的在『当前代码』上产生过；不通过则拒绝出数。",
        epilog=(
            "退出码: 0=通过(LIVE) 1=代码比事件新(NEVER_RAN) 2=无数据源(UNKNOWN) 3=用法/文件错误。"
            " UNKNOWN 绝不能当通过使用。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--event-type", required=True, help="事件类型，如 exploit.first_action")
    parser.add_argument(
        "--code",
        required=True,
        help="被测代码文件路径（相对仓库根或绝对路径），取其 mtime 与最后事件比较",
    )
    parser.add_argument(
        "--from-json",
        help="离线事件时间数组（JSON），用于无 DB 时复算；与 --db-* 互斥",
    )
    parser.add_argument("--db-host", default="127.0.0.1")
    parser.add_argument("--db-port", type=int, default=3307)
    parser.add_argument("--db-user", default="ctf_agent")
    parser.add_argument("--db-password", default="ctf_agent")
    parser.add_argument("--db-name", default="ctf_agent")
    parser.add_argument("--db-timeout", type=int, default=3, help="连接/读写超时秒数，默认 3")
    parser.add_argument(
        "--snapshot-log",
        default=DEFAULT_SNAPSHOT_LOG.as_posix(),
        help=f"溯源快照 jsonl 路径（默认 {DEFAULT_SNAPSHOT_LOG.as_posix()}；传空串关闭）",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出判定结果")
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = build_parser()
    args = parser.parse_args(argv)

    code_path = Path(args.code)
    if not code_path.is_absolute():
        code_path = ROOT / code_path
    if not code_path.is_file():
        print(f"[错误] 被测代码不存在: {code_path}", file=sys.stderr)
        print("       路径写错会让 mtime 比对变成对空文件的比对，请先确认 --code", file=sys.stderr)
        return EXIT_USAGE

    if args.from_json:
        try:
            times, source = _load_from_json(Path(args.from_json))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"[错误] 读取 --from-json 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
            return EXIT_USAGE
    else:
        times, source = _load_from_mysql(args, args.event_type)

    code_mtime = file_mtime_iso(code_path)
    exit_code = EXIT_LIVE
    try:
        verdict = assert_feature_live(
            args.event_type,
            event_times=times,
            code_path=code_path,
            data_source=source,
            strict=True,
        )
    except (MetricsDataUnavailable, FeatureNeverRan) as exc:
        verdict = exc.args[1]
        exit_code = EXIT_UNKNOWN if isinstance(exc, MetricsDataUnavailable) else EXIT_NEVER_RAN

    if args.json:
        payload = {
            "event_type": verdict.event_type,
            "status": verdict.status,
            "ok": verdict.ok,
            "exit_code": exit_code,
            "code_path": verdict.code_path,
            "code_mtime": code_mtime,
            "last_event_at": verdict.last_event_at.isoformat() if verdict.last_event_at else None,
            "event_count": verdict.event_count,
            "lag_hours": verdict.lag_hours,
            "data_source": verdict.data_source,
            "message": verdict.message,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(verdict.explain())

    _record_snapshot(args, source=source, verdict=verdict, code_mtime=code_mtime)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
