"""Mark the two flagless DASBOOK cryptography challenges.

``[5.1.3 摩斯密码][无flag]`` and ``[5.1.4 仿射密码][无flag]`` are offline puzzles
whose own titles say they carry no flag.  They still sit in ``challenges`` with
``status = ACTIVE`` and the default ``flag\\{[^}]+\\}`` pattern, so anything that
treats "ACTIVE + has a pattern" as solvable will keep spending runs on a target
that cannot produce a flag -- both already have COMPLETED_UNSOLVED runs.

The marker goes in ``metadata_json`` rather than a new column because
``upsert_local_challenge`` merges with ``{**existing.metadata_json, ...}``: keys
we add survive re-import, and no migration is needed.  ``status`` is left alone
-- it only ever holds ACTIVE/SOLVED and means something else.

Run with ``--revert`` to remove the marker.
"""

from __future__ import annotations

import argparse
import json

MARKER = "no_flag_challenge"
TARGETS = ("e16004e6-d553-481b-aaa2-3f50fafebc42", "a3f2adce-1871-4d89-9b61-af122d36a3a2")


def connect():
    import pymysql

    return pymysql.connect(
        host="127.0.0.1",
        port=3307,
        user="ctf_agent",
        password="ctf_agent",
        database="ctf_agent",
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revert", action="store_true", help="remove the marker")
    args = parser.parse_args()

    conn = connect()
    try:
        with conn.cursor() as cur:
            for challenge_id in TARGETS:
                cur.execute(
                    "SELECT id, name, metadata_json FROM challenges WHERE id = %s",
                    (challenge_id,),
                )
                row = cur.fetchone()
                if row is None:
                    print(f"  {challenge_id}: NOT FOUND, skipped")
                    continue
                raw = row["metadata_json"]
                # The column is JSON but the driver hands back a string for rows
                # written before the model default took over.
                meta = json.loads(raw) if isinstance(raw, str) else dict(raw or {})

                if args.revert:
                    meta.pop(MARKER, None)
                    action = "cleared"
                else:
                    meta[MARKER] = True
                    action = "marked"

                cur.execute(
                    "UPDATE challenges SET metadata_json = %s WHERE id = %s",
                    (json.dumps(meta, ensure_ascii=False), challenge_id),
                )
                print(f"  {row['name']}: {action}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
