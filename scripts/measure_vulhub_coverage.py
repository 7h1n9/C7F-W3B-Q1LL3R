"""Measure how many arena CVE boxes now reach a runnable recipe.

Run before and after the vulhub import to know whether the work changed
anything a Worker can use.  Reports coverage, and separates "has a recipe" from
"the recipe contains a request that can be sent" -- the distinction that made
the old 151-entry file nearly useless (32/151 runnable).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.services.known_exploit import known_exploit_block  # noqa: E402

_REQUEST_SHAPE = re.compile(r"HTTP/1\.[01]|curl\s|```(?:http|bash|sh|python)?\s*\n", re.I)


def main() -> int:
    import os
    import pymysql

    conn = pymysql.connect(
        host=os.getenv("APP_AUDIT_DB_HOST", "127.0.0.1"),
        port=int(os.getenv("APP_AUDIT_DB_PORT", "3307")),
        user=os.getenv("APP_AUDIT_DB_USER", "ctf_agent"),
        password=os.getenv("APP_AUDIT_DB_PASSWORD", "ctf_agent"),
        database=os.getenv("APP_AUDIT_DB_NAME", "ctf_agent"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
        connect_timeout=8,
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM challenges WHERE name LIKE '%CVE-%' ORDER BY name")
            titles = [row["name"] for row in cur.fetchall()]
    finally:
        conn.close()

    has_recipe: list[str] = []
    runnable: list[str] = []
    missing: list[str] = []

    for title in titles:
        block = known_exploit_block(
            SimpleNamespace(name=title, description="点击启动靶机。")
        ) or ""
        if len(block) > 200:
            has_recipe.append(title)
            if _REQUEST_SHAPE.search(block):
                runnable.append(title)
        else:
            missing.append(title)

    print(f"CVE 题共 {len(titles)} 道")
    print(f"  拿到配方      {len(has_recipe)} ({len(has_recipe) * 100 // max(1, len(titles))}%)")
    print(f"  其中含可发请求 {len(runnable)} ({len(runnable) * 100 // max(1, len(titles))}%)")
    print(f"  完全没覆盖    {len(missing)}")
    if missing:
        print("\n未覆盖（vulhub 语料里没有这个 CVE，需要另找来源）：")
        for title in missing[:14]:
            print(f"  {title[:70]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
