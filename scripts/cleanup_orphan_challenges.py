"""Delete Challenge rows that no SolveRun references.

These accumulate from throwaway harness runs (the "Codex Preflight" family) and
never carry a usable record: no Run, therefore no evidence, score or report.

The script is deliberately conservative:

  * dry run by default -- nothing is written unless ``--apply`` is passed;
  * the orphan set is derived inside the same transaction that deletes it, so the
    set cannot drift between inspection and deletion;
  * a guard refuses to run when an orphan Challenge turns out to own a SolveRun,
    because that would mean the orphan definition itself is wrong;
  * the delete order below is explicit, but the FK subtree is also discovered from
    ``information_schema`` and compared with it.  A schema change therefore stops
    the script with a precise diff instead of leaving dangling rows, deleting a
    table twice, or failing halfway through a partial delete.

Challenges that a Run references are never touched: their history is the point of
the database.

Usage::

    python scripts/cleanup_orphan_challenges.py            # report only
    python scripts/cleanup_orphan_challenges.py --apply    # delete
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DEFAULT_URL = "mysql+asyncmy://ctf_agent:ctf_agent@127.0.0.1:3307/ctf_agent"
TARGET = "challenges"

# ``(child_table, foreign_key_column, parent_table)``, children before parents.
# ``challenge_conversations`` appears as a parent because two tables point at it.
DELETE_ORDER: tuple[tuple[str, str, str], ...] = (
    ("challenge_messages", "conversation_id", "challenge_conversations"),
    ("challenge_conversation_skills", "conversation_id", "challenge_conversations"),
    ("challenge_attachments", "challenge_id", TARGET),
    ("challenge_conversations", "challenge_id", TARGET),
    ("challenge_predictions", "challenge_id", TARGET),
    ("challenge_skill_bindings", "challenge_id", TARGET),
    ("run_scores", "challenge_id", TARGET),
)
# Real execution history.  An orphan owning one aborts the run; never deleted.
NEVER_DELETE = ("solve_runs",)
# Edges that point back up the tree (or at the root itself) instead of down it.
# They are not deleted, but they can still block a delete, so they are declared
# rather than filtered away: a new one aborts instead of being ignored.
EXPECTED_BACK_REFERENCES = {("challenges", "primary_attachment_id")}

ORPHAN_CLAUSE = """
    select c.id, c.created_at
    from challenges c
    left join solve_runs r on r.challenge_id = c.id
    where r.challenge_id is null
"""


async def _discover_subtree(conn) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """FK edges under ``challenges``, transitively, split into children and back-edges."""

    children: set[tuple[str, str]] = set()
    back_references: set[tuple[str, str]] = set()
    visited = {TARGET}
    frontier = [TARGET]
    while frontier:
        parent = frontier.pop()
        rows = (
            await conn.execute(
                text(
                    "select table_name as tbl, column_name as col "
                    "from information_schema.key_column_usage "
                    "where table_schema = database() and referenced_table_name = :parent"
                ),
                {"parent": parent},
            )
        ).mappings().all()
        for row in rows:
            table, column = str(row["tbl"]), str(row["col"])
            if table in visited:
                # Already part of the subtree: this edge points up the tree, so it
                # is a constraint on the delete rather than a row to delete.
                back_references.add((table, column))
                continue
            if table in NEVER_DELETE:
                continue
            children.add((table, column))
            visited.add(table)
            frontier.append(table)
    return children, back_references


def _literal_list(values: list[str]) -> str:
    return ",".join(f"'{value}'" for value in values)


async def run(url: str, apply: bool) -> int:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            expected = {(child, column) for child, column, _parent in DELETE_ORDER}
            discovered, back_references = await _discover_subtree(conn)
            if discovered != expected:
                print(
                    "ABORT: the foreign key subtree under `challenges` no longer matches the "
                    "delete order in this script.\n"
                    f"  missing from DELETE_ORDER: {sorted(discovered - expected)}\n"
                    f"  no longer in the schema  : {sorted(expected - discovered)}",
                    file=sys.stderr,
                )
                return 2
            if back_references != EXPECTED_BACK_REFERENCES:
                print(
                    "ABORT: the back-references into `challenges` changed; review whether they "
                    "can block the delete.\n"
                    f"  missing from EXPECTED_BACK_REFERENCES: "
                    f"{sorted(back_references - EXPECTED_BACK_REFERENCES)}\n"
                    f"  no longer in the schema             : "
                    f"{sorted(EXPECTED_BACK_REFERENCES - back_references)}",
                    file=sys.stderr,
                )
                return 2

            orphans = (await conn.execute(text(ORPHAN_CLAUSE))).mappings().all()
            orphan_ids = [str(row["id"]) for row in orphans]
            if not orphan_ids:
                print("No orphan challenges. Nothing to do.")
                return 0
            print(f"orphan challenges: {len(orphan_ids)}")
            print(
                f"  created between {min(row['created_at'] for row in orphans)} "
                f"and {max(row['created_at'] for row in orphans)}"
            )
            quoted = _literal_list(orphan_ids)

            for guard in NEVER_DELETE:
                count = int(
                    (
                        await conn.execute(
                            text(f"select count(*) from `{guard}` where challenge_id in ({quoted})")
                        )
                    ).scalar()
                    or 0
                )
                if count:
                    print(
                        f"ABORT: {count} row(s) in `{guard}` reference a purported orphan "
                        "challenge; refusing to delete.",
                        file=sys.stderr,
                    )
                    return 3

            # A surviving row must not point at a doomed one through a
            # back-reference, or the delete would fail on that foreign key.
            for table, column in sorted(EXPECTED_BACK_REFERENCES):
                blocking = int(
                    (
                        await conn.execute(
                            text(
                                f"select count(*) from `{table}` as survivor "
                                f"where survivor.`{column}` in ({quoted}) "
                                f"and survivor.id not in ({quoted})"
                            )
                        )
                    ).scalar()
                    or 0
                )
                if blocking:
                    print(
                        f"ABORT: {blocking} surviving row(s) in `{table}` reference a doomed "
                        f"challenge through `{column}`; clear them first.",
                        file=sys.stderr,
                    )
                    return 4

            # Ids of the intermediate parents, required by their children.  Every
            # table here hangs off a Challenge, so the orphan set filters them.
            ids: dict[str, list[str]] = {TARGET: orphan_ids}
            for parent in sorted({parent for _c, _k, parent in DELETE_ORDER} - {TARGET}):
                rows = await conn.execute(
                    text(f"select id from `{parent}` where challenge_id in ({quoted})")
                )
                ids[parent] = [str(row[0]) for row in rows]

            plan: list[tuple[str, int]] = []
            for child, column, parent in DELETE_ORDER:
                parent_values = _literal_list(ids.get(parent) or [])
                if not parent_values:
                    plan.append((child, 0))
                    continue
                count = int(
                    (
                        await conn.execute(
                            text(
                                f"select count(*) from `{child}` where `{column}` in ({parent_values})"
                            )
                        )
                    ).scalar()
                    or 0
                )
                plan.append((child, count))

            print()
            print("rows that would be deleted:")
            for child, count in sorted(plan, key=lambda item: item[1]):
                print(f"  {count:>7}  {child}")
            print(f"  {len(orphan_ids):>7}  {TARGET}")

            if not apply:
                print()
                print("Dry run: nothing was deleted.  Re-run with --apply to delete.")
                return 0

            for child, column, parent in DELETE_ORDER:
                parent_values = _literal_list(ids.get(parent) or [])
                if not parent_values:
                    continue
                await conn.execute(
                    text(f"delete from `{child}` where `{column}` in ({parent_values})")
                )
            result = await conn.execute(text(f"delete from `{TARGET}` where id in ({quoted})"))
            print()
            print(f"deleted {result.rowcount} challenge(s)")

            remaining = (
                await conn.execute(
                    text(
                        "select count(*) from challenges c left join solve_runs r "
                        "on r.challenge_id = c.id where r.challenge_id is null"
                    )
                )
            ).scalar()
            runs_left = (await conn.execute(text("select count(*) from solve_runs"))).scalar()
            print(f"orphan challenges remaining: {remaining}")
            print(f"solve_runs preserved: {runs_left}")
            return 0
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("APP_DATABASE_URL", DEFAULT_URL),
        help="SQLAlchemy async URL (defaults to APP_DATABASE_URL)",
    )
    args = parser.parse_args()
    return asyncio.run(run(args.database_url, args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
