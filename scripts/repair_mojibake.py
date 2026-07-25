from __future__ import annotations

import argparse
import codecs
import json
import re
import sys
from collections import defaultdict

import pymysql

sys.setrecursionlimit(100000)


MOJIBAKE_MARKERS = set("璧勪骇淇濅慨鏍搁獙骞冲彴銆锛绱閿锛濅紞").union(
    "縺ゅぃ繧峨※繧峨せ"  # common UTF-8/GBK mojibake fragments
)


def build_inverse_gbk() -> dict[str, list[bytes]]:
    inverse: dict[str, set[bytes]] = defaultdict(set)
    for value in range(0x00, 0x100):
        raw = bytes([value])
        try:
            inverse[raw.decode("gbk")].add(raw)
        except UnicodeDecodeError:
            pass
    for first in range(0x81, 0x100):
        for second in range(0x40, 0x100):
            if second == 0x7F:
                continue
            raw = bytes([first, second])
            try:
                inverse[raw.decode("gbk")].add(raw)
            except UnicodeDecodeError:
                pass
    return {key: sorted(values) for key, values in inverse.items()}


def valid_utf8_prefix(raw: bytes) -> bool:
    try:
        raw.decode("utf-8")
        return True
    except UnicodeDecodeError as exc:
        return exc.reason == "unexpected end of data" and exc.end == len(raw)


def has_mojibake_signal(value: str) -> bool:
    return any(marker in value for marker in MOJIBAKE_MARKERS) or any(
        0xE000 <= ord(char) <= 0xF8FF or 0x2000 <= ord(char) <= 0x33FF
        for char in value
    )


def recover_gbk_mojibake(value: str, inverse: dict[str, list[bytes]]) -> str | None:
    # Keep a broad signal gate to prevent ordinary Chinese text from being
    # reinterpreted as another valid string on later runs.
    if not value or not has_mojibake_signal(value):
        return None
    options: list[list[bytes]] = []
    for char in value:
        candidates = inverse.get(char)
        if not candidates:
            return None
        options.append(candidates)

    def search(index: int, raw: bytes) -> bytes | None:
        if index == len(options):
            try:
                raw.decode("utf-8")
                return raw
            except UnicodeDecodeError:
                return None
        for candidate in options[index]:
            combined = raw + candidate
            if valid_utf8_prefix(combined):
                found = search(index + 1, combined)
                if found is not None:
                    return found
        return None

    recovered = search(0, b"")
    if recovered is None:
        return None
    result = recovered.decode("utf-8")
    return result if result != value and "�" not in result else None


def recover_gb18030_mojibake(value: str) -> str | None:
    """Reverse a full UTF-8 -> GB18030 decode round-trip when lossless."""
    if not value:
        return None
    # A normal Chinese string can occasionally also happen to form valid
    # UTF-8 after a GB18030 encode.  Require the fingerprints produced by the
    # broken conversion (private-use/compatibility symbols or known fragments)
    # so a second run cannot mutate legitimate text such as ``目录``.
    if not has_mojibake_signal(value):
        return None
    try:
        result = value.encode("gb18030").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return None
    return result if result != value and "�" not in result else None


def recover_text(value: str, inverse: dict[str, list[bytes]]) -> str | None:
    return recover_gbk_mojibake(value, inverse) or recover_gb18030_mojibake(value)


def repair_json(value: str, inverse: dict[str, list[bytes]]) -> tuple[str, bool]:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return value, False

    changed = False

    def visit(node):
        nonlocal changed
        if isinstance(node, str):
            repaired = recover_text(node, inverse)
            if repaired is not None:
                changed = True
                return repaired
            return node
        if isinstance(node, list):
            return [visit(item) for item in node]
        if isinstance(node, dict):
            return {key: visit(item) for key, item in node.items()}
        return node

    repaired = visit(parsed)
    if not changed:
        return value, False
    return json.dumps(repaired, ensure_ascii=False, separators=(",", ":")), True


def qi(identifier: str) -> str:
    return "`" + identifier.replace("`", "``") + "`"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--table", action="append", default=[])
    args = parser.parse_args()

    inverse = build_inverse_gbk()
    conn = pymysql.connect(
        host="127.0.0.1", port=3307, user="ctf_agent", password="ctf_agent",
        database="ctf_agent", charset="utf8mb4", autocommit=True,
    )
    cur = conn.cursor()
    changed = 0
    by_table: dict[str, int] = defaultdict(int)
    try:
        cur.execute("SHOW TABLES")
        tables = [row[0] for row in cur.fetchall()]
        if args.table:
            tables = [table for table in tables if table in args.table]
        for table in tables:
            cur.execute(f"SHOW COLUMNS FROM {qi(table)}")
            columns = cur.fetchall()
            text_columns = [
                (row[0], row[1].lower())
                for row in columns
                if row[1].lower().startswith(("varchar", "char", "text", "mediumtext", "longtext", "json"))
            ]
            if not text_columns:
                continue
            primary = [row[0] for row in columns if row[3] == "PRI"]
            if len(primary) != 1:
                continue
            key = primary[0]
            select_columns = [key] + [name for name, _ in text_columns]
            cur.execute(
                f"SELECT {', '.join(qi(name) for name in select_columns)} "
                f"FROM {qi(table)}"
            )
            rows = cur.fetchall()
            for row in rows:
                key_value = row[0]
                for offset, (column, sql_type) in enumerate(text_columns, start=1):
                    value = row[offset]
                    if not isinstance(value, str):
                        continue
                    if sql_type.startswith("json"):
                        repaired, did_change = repair_json(value, inverse)
                    else:
                        repaired = recover_text(value, inverse)
                        did_change = repaired is not None
                    if not did_change:
                        continue
                    changed += 1
                    by_table[table] += 1
                    if changed <= 20:
                        print(f"{table}.{column}: {ascii(value[:80])} -> {ascii(repaired[:80])}")
                    if args.apply:
                        cur.execute(
                            f"UPDATE {qi(table)} SET {qi(column)}=%s WHERE {qi(key)}=%s",
                            (repaired, key_value),
                        )
                    if args.limit and changed >= args.limit:
                        break
                if args.limit and changed >= args.limit:
                    break
            if args.limit and changed >= args.limit:
                break
    finally:
        cur.close()
        conn.close()
    mode = "APPLIED" if args.apply else "PREVIEW"
    print(f"MODE={mode} CHANGED={changed}")
    for table, count in sorted(by_table.items()):
        print(f"TABLE={table} CHANGED={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
