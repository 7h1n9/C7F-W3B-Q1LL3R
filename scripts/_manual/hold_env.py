"""One-off instrument: start an arena environment and *hold* it for manual poking.

``_probe_env.py`` starts a box and deletes it as soon as it is ready, which is the
right shape for watching platform health but useless when the question is "does
this CVE chain actually work on this box".  This variant starts the environment,
prints the access URL, and then waits -- so a human (or a curl loop) can drive the
PoC by hand without a worker burning a Run budget, and without the queue's
single-slot bookkeeping.

    env -u PYTHONPATH PYTHONPATH=. ../.venv3.11/Scripts/python.exe _hold_env.py weblogic2894 --hold 900
    env -u PYTHONPATH PYTHONPATH=. ../.venv3.11/Scripts/python.exe _hold_env.py weblogic2894 --delete
"""

from __future__ import annotations

import argparse
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


async def _client():
    async with SessionLocal() as session:
        conn = (await session.execute(select(ArenaConnection).limit(1))).scalar_one()
        token = decrypt_api_key(conn.encrypted_token) if conn.encrypted_token else None
        return await arena_mcp_clients.get(_Conn(conn.id, conn.base_url), token)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("challenge", nargs="?", default="weblogic2894")
    parser.add_argument("--hold", type=int, default=600)
    parser.add_argument("--delete", action="store_true")
    args = parser.parse_args()
    challenge_id = CHALLENGES.get(args.challenge, args.challenge)

    client = await _client()

    if args.delete:
        out = await client.delete_environment(
            practice_ground_id=PG, challenge_id=challenge_id
        )
        print("delete ->", json.dumps(out, ensure_ascii=False)[:300])
        return

    started = await client.start_environment(
        practice_ground_id=PG, challenge_id=challenge_id
    )
    print("start ->", json.dumps(started, ensure_ascii=False)[:400], flush=True)

    url = None
    for tick in range(40):
        await asyncio.sleep(3)
        state = _env(
            await client.get_environment(
                practice_ground_id=PG, challenge_id=challenge_id
            )
        )
        print(
            f"[{tick * 3:>3}s] status={state.get('status')!r} "
            f"ready={state.get('access_ready')!r} url={state.get('access_url')!r} "
            f"remaining={state.get('remaining_seconds')!r}",
            flush=True,
        )
        if state.get("access_ready") and state.get("access_url"):
            url = state["access_url"]
            break
        if state.get("status") in {"failed", "error", "deleted"}:
            break

    if not url:
        print("NOT READY -- giving up", flush=True)
        return
    print(f"HOLDING {url} for {args.hold}s -- delete with --delete", flush=True)
    await asyncio.sleep(args.hold)
    print("hold over; box left running, delete explicitly with --delete", flush=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
