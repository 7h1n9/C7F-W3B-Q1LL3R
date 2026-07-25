from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pymysql


@dataclass(frozen=True)
class Column:
    name: str
    sql_type: str

    @property
    def kind(self) -> str:
        base = self.sql_type.lower().split("(")[0]
        if base in {"tinyint", "smallint", "mediumint", "int", "integer", "bigint", "decimal", "numeric", "float", "double", "real", "bit", "bool", "boolean"}:
            return "number"
        if base in {"date", "datetime", "timestamp", "time", "year"}:
            return "date"
        return "text"


def dump_statements(text: str):
    buffer: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--") or stripped.startswith("/*"):
            continue
        if stripped.upper().startswith("INSERT INTO "):
            yield stripped[:-1] if stripped.endswith(";") else stripped
            continue
        buffer.append(line)
        if stripped.endswith(";"):
            statement = "\n".join(buffer).strip()[:-1]
            buffer = []
            if statement:
                yield statement
    tail = "\n".join(buffer).strip()
    if tail:
        yield tail


def skip_space(text: str, position: int) -> int:
    while position < len(text) and text[position].isspace():
        position += 1
    return position


def delimiter(text: str, position: int):
    position = skip_space(text, position)
    if position >= len(text):
        return None
    if text[position] == ",":
        return "comma", position + 1
    if text[position] == ")":
        return "close", position + 1
    return None


def likely_next_value(text: str, position: int, next_column: Column | None) -> bool:
    position = skip_space(text, position)
    if position >= len(text):
        return next_column is None
    if text[position] == "'":
        return True
    if text[position : position + 4].upper() == "NULL":
        return True
    if next_column is not None and next_column.kind == "number":
        return text[position].isdigit() or text[position] in "+-"
    return False


def valid_unquoted(value: str, column: Column) -> bool:
    value = value.strip()
    if value.upper() == "NULL":
        return True
    if column.kind == "number":
        return bool(re.fullmatch(r"[-+]?\d+(?:\.\d+)?", value))
    return True


def parse_tuple(source: str, start: int, columns: tuple[Column, ...]):
    if start >= len(source) or source[start] != "(":
        return None

    @lru_cache(maxsize=None)
    def parse_fields(position: int, index: int):
        position = skip_space(source, position)
        if index == len(columns):
            if position < len(source) and source[position] == ")":
                return ")", position + 1
            return None
        if position >= len(source):
            return None
        column = columns[index]
        next_column = columns[index + 1] if index + 1 < len(columns) else None

        if source[position] != "'":
            end = position
            while end < len(source) and source[end] not in ",)":
                end += 1
            value = source[position:end].rstrip()
            if not valid_unquoted(value, column):
                return None
            sep = delimiter(source, end)
            if sep is None:
                return None
            kind, next_position = sep
            if kind == "comma" and next_column is not None:
                rest = parse_fields(next_position, index + 1)
                return (value + "," + rest[0], rest[1]) if rest else None
            if kind == "close" and next_column is None:
                rest = parse_fields(end, index + 1)
                return (value + rest[0], rest[1]) if rest else None
            return None

        output = ["'"]
        cursor = position + 1
        while cursor < len(source):
            char = source[cursor]
            if char == "\\" and cursor + 1 < len(source):
                output.extend((char, source[cursor + 1]))
                cursor += 2
                continue

            if char == "'":
                sep = delimiter(source, cursor + 1)
                if sep is not None:
                    kind, next_position = sep
                    if kind == "comma" and next_column is not None:
                        if likely_next_value(source, next_position, next_column):
                            rest = parse_fields(next_position, index + 1)
                            if rest is not None:
                                return ("".join(output) + "'," + rest[0], rest[1])
                    elif kind == "close" and next_column is None:
                        rest = parse_fields(cursor + 1, index + 1)
                        if rest is not None:
                            return ("".join(output) + "'" + rest[0], rest[1])
                output.extend(("\\", "'"))
                cursor += 1
                continue

            if char in ",)" and likely_next_value(source, cursor + 1, next_column):
                if char == "," and next_column is not None:
                    rest = parse_fields(cursor + 1, index + 1)
                    if rest is not None:
                        return ("".join(output) + "'," + rest[0], rest[1])
                if char == ")" and next_column is None:
                    rest = parse_fields(cursor, index + 1)
                    if rest is not None:
                        return ("".join(output) + "'" + rest[0], rest[1])

            output.append(char)
            cursor += 1
        return None

    parsed = parse_fields(start + 1, 0)
    if parsed is None:
        return None
    return "(" + parsed[0], parsed[1]


def parse_insert(statement: str, columns: tuple[Column, ...]):
    match = re.search(r"\bVALUES\b", statement, re.IGNORECASE)
    if match is None:
        return [], None
    source = statement[match.end() :].strip()
    rows: list[str] = []
    position = 0
    while position < len(source):
        position = skip_space(source, position)
        parsed = parse_tuple(source, position, columns)
        if parsed is None:
            return rows, f"cannot parse tuple at offset {position}"
        row, position = parsed
        rows.append(row)
        position = skip_space(source, position)
        if position < len(source):
            if source[position] != ",":
                return rows, f"expected row separator at offset {position}"
            position += 1
    return rows, None


def split_row_values(row: str) -> list[str] | None:
    if len(row) < 2 or row[0] != "(" or row[-1] != ")":
        return None
    values: list[str] = []
    start = 1
    cursor = 1
    quote = False
    escaped = False
    while cursor < len(row) - 1:
        char = row[cursor]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "'":
                quote = False
        elif char == "'":
            quote = True
        elif char == ",":
            values.append(row[start:cursor])
            start = cursor + 1
        cursor += 1
    values.append(row[start:-1])
    return values


def fallback_row(row: str, columns: tuple[Column, ...], mode: str) -> str | None:
    values = split_row_values(row)
    if values is None or len(values) != len(columns):
        return None
    repaired: list[str] = []
    for value, column in zip(values, columns):
        stripped = value.strip()
        if mode == "json" and column.sql_type.lower().startswith("json") and stripped.upper() != "NULL":
            lowered = column.name.lower()
            literal = "[]" if any(token in lowered for token in ("list", "tools", "skills", "hosts")) else "{}"
            repaired.append(f"'{literal}'")
        elif mode == "date" and column.kind == "date" and stripped in {"", "''"}:
            repaired.append("'1970-01-01 00:00:00'")
        else:
            repaired.append(value)
    return "(" + ",".join(repaired) + ")"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dump", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3307)
    parser.add_argument("--user", default="ctf_agent")
    parser.add_argument("--password", default="ctf_agent")
    parser.add_argument("--database", default="ctf_agent")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    text = args.dump.read_text(encoding="utf-16")
    connection = pymysql.connect(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        database=args.database,
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=15,
        read_timeout=600,
        write_timeout=600,
    )
    cursor = connection.cursor()
    schemas: dict[str, tuple[Column, ...]] = {}
    inserted = 0
    skipped = 0
    try:
        if cursor and not args.dry_run:
            cursor.execute("SET FOREIGN_KEY_CHECKS=0")
        for statement in dump_statements(text):
            match = re.match(r"INSERT\s+INTO\s+`([^`]+)`\s+VALUES\b", statement, re.IGNORECASE)
            if not match:
                if cursor and not args.dry_run and statement:
                    cursor.execute(statement)
                continue
            table = match.group(1)
            if table not in schemas:
                if cursor:
                    cursor.execute(f"SHOW COLUMNS FROM `{table}`")
                    schemas[table] = tuple(Column(row[0], row[1]) for row in cursor.fetchall())
                else:
                    raise RuntimeError("database schema unavailable")
            rows, parse_error = parse_insert(statement, schemas[table])
            if parse_error:
                print(f"PARTIAL_TABLE={table} reason={parse_error}", file=sys.stderr)
                skipped += 1
            if args.dry_run:
                inserted += len(rows)
                continue
            prefix = f"INSERT INTO `{table}` VALUES "
            for row in rows:
                try:
                    cursor.execute(prefix + row)
                    inserted += 1
                except Exception as exc:
                    code = exc.args[0] if exc.args else None
                    fallback = None
                    if code == 3140:
                        fallback = fallback_row(row, schemas[table], "json")
                    elif code == 1292:
                        fallback = fallback_row(row, schemas[table], "date")
                    if fallback is not None:
                        try:
                            cursor.execute(prefix + fallback)
                            inserted += 1
                            continue
                        except Exception:
                            pass
                    print(f"SKIP_ROW={table} reason={exc}", file=sys.stderr)
                    skipped += 1
        if cursor and not args.dry_run:
            cursor.execute("SET FOREIGN_KEY_CHECKS=1")
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()
    print(f"INSERTED={inserted} SKIPPED={skipped}")
    return 0 if skipped == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
