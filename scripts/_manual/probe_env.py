"""One-off probe: watch one arena environment from start to ready.

Not part of the shipped surface -- this is the manual instrument the batch is
debugged with when ``ARENA_ENVIRONMENT_FAILED`` starts showing up in the queue.
It bypasses ``ArenaLeaseManager`` on purpose so the platform can be read without
the queue's single-slot bookkeeping, and it always destroys what it started.

Run from ``backend/`` with:

    env -u PYTHONPATH PYTHONPATH=. ../.venv3.11/Scripts/python.exe _probe_env.py <challenge-id>
"""

from __future__ import annotations

import asyncio
import json
import sys

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models.arena import ArenaConnection
from app.services.arena_mcp import arena_mcp_clients
from app.services.crypto import decrypt_api_key

PG = "2de971ac-26fe-448a-8719-01829e52c1d5"
CHALLENGES = {
    "phpmyadmin": "1350ef56-7d00-42b1-a538-412f4ef32783",
    "spring": "a09e5e0f-3add-49ca-a612-d522e21554a4",
    "rails": "069fb1ef-1f22-4de4-9856-7f07f0747d93",
    "weblogic2894": "8dfcf70d-3416-4f92-8aa5-ac44beea8bfb",
    "weblogic2628": "a4276c0b-3c46-4ce8-9ca1-8b27d65601f4",
}


class _Conn:
    def __init__(self, id_: str, base: str) -> None:
        self.id = id_
        self.base_url = base
        self.enabled = True
        self.encrypted_token = None


def _env(value: object) -> dict:
    if isinstance(value, dict):
        inner = value.get("environment")
        if isinstance(inner, dict):
            return inner
    return {}


async def main() -> None:
    key = sys.argv[1] if len(sys.argv) > 1 else "phpmyadmin"
    challenge_id = CHALLENGES.get(key, key)

    async with SessionLocal() as session:
        conn = (await session.execute(select(ArenaConnection).limit(1))).scalar_one()
        token = decrypt_api_key(conn.encrypted_token) if conn.encrypted_token else None
        cid, base = conn.id, conn.base_url

    client = await arena_mcp_clients.get(_Conn(cid, base), token)

    started = await client.start_environment(
        practice_ground_id=PG, challenge_id=challenge_id
    )
    print("start ->", json.dumps(started, ensure_ascii=False)[:500])

    for tick in range(40):
        await asyncio.sleep(3)
        state = _env(
            await client.get_environment(
                practice_ground_id=PG, challenge_id=challenge_id
            )
        )
        print(
            f"[{tick * 3:>3}s] status={state.get('status')!r} "
            f"ready={state.get('access_ready')!r} "
            f"url={state.get('access_url')!r} "
            f"remaining={state.get('remaining_seconds')!r}"
        )
        if state.get("access_ready") and state.get("access_url"):
            break
        if state.get("status") in {"failed", "error", "deleted"}:
            break

    try:
        deleted = await client.delete_environment(
            practice_ground_id=PG, challenge_id=challenge_id
        )
        print("delete ->", json.dumps(deleted, ensure_ascii=False)[:300])
    except Exception as error:  # noqa: BLE001 - probe: report, never raise
        print("delete failed ->", error)


if __name__ == "__main__":
    asyncio.run(main())
