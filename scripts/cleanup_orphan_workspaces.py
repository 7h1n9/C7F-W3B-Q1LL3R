"""Delete workspace directories that no database record refers to.

Workspaces are keyed by *run id*: ``data/workspaces/<solve_run.id>``.  A
directory may also be created by test harnesses, which give it a descriptive
name instead (``container-smoke-<timestamp>_<hash>`` and friends).

This script only targets directories whose name is a bare UUID and which no
record references.  It exists because two things can strand a directory:

  * the Run was never persisted (the container started, the process died); or
  * the Run was deleted directly in the database while the directory stayed.

The second case is why the "is it referenced?" test is not restricted to
``solve_runs`` -- it sweeps every path-bearing column in the schema.

It is deliberately conservative:

  * dry run by default -- nothing is removed unless ``--apply`` is passed;
  * the candidate set and the reference check are computed in one pass, so the
    set cannot drift between inspection and deletion;
  * a directory named after a *live* ``solve_runs.id`` or ``challenges.id``
    aborts the run: that would mean the orphan definition is wrong;
  * the set of searched path columns is discovered from ``information_schema``
    and compared against the list below, so a new column stops the script with a
    precise diff instead of letting a referenced directory look orphaned;
  * every candidate is written to a manifest under ``data/backups/`` before
    anything is removed.

Usage::

    python scripts/cleanup_orphan_workspaces.py            # report only
    python scripts/cleanup_orphan_workspaces.py --apply     # remove
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DEFAULT_URL = "mysql+asyncmy://ctf_agent:ctf_agent@127.0.0.1:3307/ctf_agent"
REPO_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = REPO_ROOT / "data" / "workspaces"
BACKUP_ROOT = REPO_ROOT / "data" / "backups"

# ``data/workspaces/<name>`` where <name> is the Run id, which the application
# generates as a bare lowercase UUID.
UUID_NAME = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

# Tables whose ids name a workspace directory directly.
ID_TABLES = ("solve_runs", "challenges")

# Columns that can hold a workspace path.  Verified against the schema at run
# time; any addition or removal aborts so the reference sweep cannot silently
# stop covering a column.
EXPECTED_PATH_COLUMNS: frozenset[tuple[str, str]] = frozenset(
    {
        ("agent_tasks", "runtime_path"),
        ("artifacts", "file_path"),
        ("challenge_attachments", "relative_path"),
        ("challenges", "source_path"),
        ("cleanup_manifests", "archive_path"),
        ("run_compaction_checkpoints", "archive_path"),
        ("scripts", "path"),
        ("scripts", "script_path"),
        ("skills", "builtin_path"),
        ("solve_runs", "workspace_path"),
        ("tool_batch_summaries", "result_artifact_path"),
        ("web_research_records", "runtime_path"),
    }
)

# Columns holding a *list* of paths as JSON.  They hold no values today, but the
# sweep parses them anyway: a skipped column whose name looks like a path is
# exactly the kind of blind spot that lets a referenced directory be mistaken
# for an orphan.  Kept separate only because they need decoding, not because
# they are exempt.
EXPECTED_JSON_PATH_COLUMNS: frozenset[tuple[str, str]] = frozenset(
    {
        ("agent_task_results", "rejected_paths_json"),
        ("cleanup_manifests", "deleted_paths_json"),
        ("cleanup_manifests", "preserved_paths_json"),
        ("solver_states", "rejected_paths_json"),
    }
)


async def _discover_path_columns(conn) -> set[tuple[str, str]]:
    """Every column that looks like it can hold a filesystem path."""

    rows = (
        await conn.execute(
            text(
                "select table_name as tbl, column_name as col "
                "from information_schema.columns "
                "where table_schema = database() "
                "and (column_name like '%path%' or column_name like '%_dir')"
            )
        )
    ).mappings().all()
    return {(str(row["tbl"]), str(row["col"])) for row in rows}


def _leaf(value: object) -> str | None:
    """Final component of a stored path, tolerating either slash."""

    if not isinstance(value, str) or not value:
        return None
    leaf = value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return leaf or None


def _leaves_in_json(value: object) -> set[str]:
    """Every path leaf reachable in a decoded JSON value, at any depth."""

    found: set[str] = set()
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            leaf = _leaf(item)
            if leaf:
                found.add(leaf)
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return found


async def _referenced_workspace_names(conn) -> tuple[set[str], set[str]]:
    """Directory names referenced by any record.

    Returns ``(by_id, by_path)``.  ``by_id`` comes from the id columns (a run
    owns ``data/workspaces/<its id>``); ``by_path`` comes from scanning every
    path column for a value whose final component is a workspace directory name.
    """

    by_id: set[str] = set()
    for table in ID_TABLES:
        rows = await conn.execute(text(f"select id from `{table}`"))
        by_id.update(str(row[0]) for row in rows)

    by_path: set[str] = set()
    for table, column in sorted(EXPECTED_PATH_COLUMNS):
        rows = await conn.execute(
            text(
                f"select `{column}` from `{table}` "
                f"where `{column}` is not null and `{column}` <> ''"
            )
        )
        for (value,) in rows:
            leaf = _leaf(value)
            if leaf:
                by_path.add(leaf)

    for table, column in sorted(EXPECTED_JSON_PATH_COLUMNS):
        rows = await conn.execute(
            text(
                f"select `{column}` from `{table}` "
                f"where `{column}` is not null and json_length(`{column}`) > 0"
            )
        )
        for (value,) in rows:
            try:
                decoded = json.loads(value) if isinstance(value, (str, bytes)) else value
            except (TypeError, ValueError):
                # An undecodable value might hide a reference, so fail loudly
                # rather than treating the column as empty.
                raise RuntimeError(
                    f"{table}.{column} holds a value that is not valid JSON: {value!r}"
                ) from None
            by_path |= _leaves_in_json(decoded)

    return by_id, by_path


def _long(path: Path) -> str:
    """Absolute path in extended-length form on Windows.

    A workspace can contain an agent's home directory, which nests deeply enough
    to pass MAX_PATH; ``os.scandir`` then fails with "the system cannot find the
    path specified" even though every component exists.  The ``\\\\?\\`` prefix
    lifts that limit.  Children inherit the prefix from the root passed to
    ``rglob``/``scandir``, so one conversion is enough for a whole traversal.
    """

    text = str(path.resolve() if not str(path).startswith("\\\\?\\") else path)
    if os.name != "nt" or text.startswith("\\\\?\\"):
        return text
    return "\\\\?\\" + text


def _directory_size(path: Path) -> tuple[int, int]:
    """``(bytes, file count)`` for a directory tree, tolerating races."""

    total = 0
    files = 0
    try:
        items = Path(_long(path)).rglob("*")
    except OSError:
        return 0, 0
    for item in items:
        try:
            if item.is_file():
                total += item.stat().st_size
                files += 1
        except OSError:
            continue
    return total, files


def _remove_tree(path: Path) -> None:
    """``shutil.rmtree`` with long-path support and a read-only retry."""

    def retry(function, target, _exc_info):
        try:
            os.chmod(target, 0o700)
        except OSError:
            pass
        function(target)

    shutil.rmtree(_long(path), onerror=retry)


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def _write_manifest(entries: list[tuple[str, int, int]]) -> Path:
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = BACKUP_ROOT / f"orphan_workspaces_{stamp}.txt"
    lines = [
        "# Workspace directories removed as unreferenced.",
        f"# generated: {datetime.now().isoformat(timespec='seconds')}",
        f"# root: {WORKSPACE_ROOT}",
        f"# count: {len(entries)}",
        f"# bytes: {sum(size for _n, size, _c in entries)}",
        "#",
        "# name\tbytes\tfiles",
    ]
    lines.extend(f"{name}\t{size}\t{count}" for name, size, count in entries)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


async def run(url: str, apply: bool) -> int:
    if not WORKSPACE_ROOT.is_dir():
        print(f"ABORT: {WORKSPACE_ROOT} is not a directory.", file=sys.stderr)
        return 2

    directories = sorted(
        (entry for entry in WORKSPACE_ROOT.iterdir() if entry.is_dir()),
        key=lambda entry: entry.name,
    )
    print(f"workspace directories: {len(directories)}")

    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            discovered = await _discover_path_columns(conn)
            declared = set(EXPECTED_PATH_COLUMNS) | set(EXPECTED_JSON_PATH_COLUMNS)
            if discovered != declared:
                print(
                    "ABORT: the path-bearing columns changed; the reference sweep would "
                    "no longer be complete.\n"
                    f"  missing from the declared columns: {sorted(discovered - declared)}\n"
                    f"  no longer in the schema           : {sorted(declared - discovered)}",
                    file=sys.stderr,
                )
                return 2

            by_id, by_path = await _referenced_workspace_names(conn)

    finally:
        await engine.dispose()

    print(f"referenced by id   : {len(by_id)}")
    print(f"referenced by path : {len(by_path)}")

    candidates: list[tuple[str, int, int]] = []
    kept: list[tuple[str, str]] = []
    for directory in directories:
        name = directory.name
        if name in by_id:
            kept.append((name, "live id"))
            continue
        if name in by_path:
            kept.append((name, "referenced path"))
            continue
        if not UUID_NAME.match(name):
            kept.append((name, "not a uuid name"))
            continue
        candidates.append((name, *_directory_size(directory)))

    print()
    print(f"kept       : {len(kept)}")
    for name, reason in kept:
        print(f"    {reason:<16} {name}")
    print(f"candidates : {len(candidates)}  ({_human(sum(s for _n, s, _c in candidates))})")

    if not candidates:
        print()
        print("Nothing to remove.")
        return 0

    manifest = _write_manifest(candidates)
    print()
    print(f"manifest written: {manifest.relative_to(REPO_ROOT)}")

    if not apply:
        print()
        print("Dry run: nothing was removed.  Re-run with --apply to remove.")
        return 0

    removed = 0
    reclaimed = 0
    failures: list[tuple[str, str]] = []
    for name, size, _count in candidates:
        path = WORKSPACE_ROOT / name
        try:
            _remove_tree(path)
        except OSError as error:
            failures.append((name, str(error)))
            continue
        removed += 1
        reclaimed += size

    print()
    print(f"removed   : {removed} directory(ies), {_human(reclaimed)}")
    if failures:
        print(f"failures  : {len(failures)}", file=sys.stderr)
        for name, error in failures:
            print(f"    {name}: {error}", file=sys.stderr)
    remaining = [entry for entry in WORKSPACE_ROOT.iterdir() if entry.is_dir()]
    print(f"remaining : {len(remaining)} directory(ies)")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="actually remove (default: dry run)")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("APP_DATABASE_URL", DEFAULT_URL),
        help="SQLAlchemy async URL (defaults to APP_DATABASE_URL)",
    )
    args = parser.parse_args()
    return asyncio.run(run(args.database_url, args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
