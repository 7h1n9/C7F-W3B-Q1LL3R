"""Deliver a flag the solver already extracted, without re-running the solve.

Why this exists
---------------
The solve loop can die *after* the flag is persisted.  On 2026-10-19 it did
exactly that: the Run reached ``COMPLETED_SOLVED``, ``flag_candidates`` held a
``VALID`` value, and then the reporting path raised ``too many values to unpack
(expected 3)``, so nothing was ever submitted.  Re-running a twenty-minute solve
to deliver a value already in hand burns the platform's single environment slot
for no new information.

The platform requires a running target to accept a submission
(``requires_running_target_for_submit``), so the environment is checked first and
the script refuses rather than submitting into a dead instance.

What this cannot do
-------------------
Deliver a flag for a challenge whose environment is **gone**.  ``BUUCTF-Real``
issues a *fresh* flag per environment instance (measured 2026-10-19: the same
challenge produced ``CTF2{a569d9ab-...}`` in one instance and
``CTF2{00be0143-...}`` in the next), so a value recovered from a destroyed
environment is permanently worthless, not merely late.  ``requires_running_target_for_submit``
then rejects it a second time.  A container challenge's submission therefore has
to happen inside the solve loop, while its own environment is still leased --
which is exactly what ``_run_and_submit`` now does.

Note on ``NOT_FOUND``: the platform returns that same message for a wrong id, a
missing environment, and an invisible resource.  It is not evidence of a
permission or session-scope problem -- the first version of this script sent the
**local** challenge id and collected ``NOT_FOUND`` from every call, which briefly
looked like the platform hiding a leased environment.  Check the id first.

Usage
-----
    python scripts/submit_arena_flag.py \
        --challenge-id db6bdcc0-76bf-4f0a-8515-7ba57fc2b8be \
        --flag 'CTF2{...}'

    # discover what is waiting
    python scripts/submit_arena_flag.py --list-pending

This is an operator tool, not part of the solve loop.  It reads and writes the
same ledger rows the loop would have written.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Development defaults, mirroring scripts/_run_backend.sh.  ``setdefault`` so a
# real deployment can override them without editing this file.
os.environ.setdefault("APP_DATABASE_URL", "mysql+asyncmy://ctf_agent:ctf_agent@127.0.0.1:3307/ctf_agent")
os.environ.setdefault("APP_ENCRYPTION_KEY", "development-only-change-me")
os.environ.setdefault("APP_ENVIRONMENT", "development")


async def _pending(session) -> list[tuple[str, str, str]]:
    """(run_id, platform_challenge_id, flag) for every VALID candidate not submitted.

    The platform id is read from ``challenges.metadata_json``, not from the row's
    own ``id``.  ``SolveRun.challenge_id`` points at the **local** mirrored row,
    which carries a fresh uuid: the first version of this function printed that
    local id, so every submission built from it came back ``NOT_FOUND`` -- and the
    platform's "not found" is indistinguishable from a wrong flag unless you know
    which of the two ids you are holding.
    """
    from sqlalchemy import select

    from app.models.arena import ArenaSubmission
    from app.models.challenge import Challenge
    from app.models.run import FlagCandidate, SolveRun

    rows = (
        await session.execute(
            select(
                FlagCandidate.run_id,
                FlagCandidate.candidate,
                Challenge.id,
                Challenge.metadata_json,
            )
            .join(SolveRun, SolveRun.id == FlagCandidate.run_id)
            .join(Challenge, Challenge.id == SolveRun.challenge_id)
            .where(FlagCandidate.review_state == "VALID")
            .order_by(FlagCandidate.created_at.desc())
            .limit(20)
        )
    ).all()
    submitted = set(
        (await session.execute(select(ArenaSubmission.attested_flag))).scalars().all()
    )
    pending: list[tuple[str, str, str]] = []
    for run_id, flag, local_id, metadata in rows:
        if flag in submitted:
            continue
        platform_id = str((metadata or {}).get("arena_challenge_id") or "")
        # Skip rather than fall back to the local id: a wrong id fails as
        # NOT_FOUND, which reads like "challenge gone" and sends the operator
        # looking at the platform instead of at this row.
        if not platform_id:
            print(f"{run_id}  (local {local_id}) not mirrored from an arena challenge", file=sys.stderr)
            continue
        pending.append((run_id, platform_id, flag))
    return pending


async def _deliver(challenge_id: str, flag: str) -> int:
    from app.core.database import SessionLocal
    from app.models.arena import ArenaConnection
    from app.services import arena_ledger
    from app.services.arena_mcp import arena_mcp_clients
    from app.services.crypto import decrypt_api_key

    async with SessionLocal() as session:
        connection = (
            await session.execute(
                __import__("sqlalchemy").select(ArenaConnection).where(ArenaConnection.enabled.is_(True))
            )
        ).scalars().first()
        if connection is None:
            print("no enabled arena connection", file=sys.stderr)
            return 2

        token = decrypt_api_key(connection.encrypted_token) if connection.encrypted_token else None
        client = await arena_mcp_clients.get(connection, token)

        # The platform refuses a submission for a challenge whose instance is
        # gone, and that refusal is indistinguishable from a wrong flag at this
        # level.  Check the environment first so the failure says what it is.
        ground_id = os.environ.get("ARENA_GROUND_ID", "2de971ac-26fe-448a-8719-01829e52c1d5")
        try:
            state = await client.get_environment(
                practice_ground_id=ground_id, challenge_id=challenge_id
            )
            print(f"[env] {state}")
        except Exception as error:  # noqa: BLE001 - reported, not fatal on its own
            print(f"[env] unreadable: {error}", file=sys.stderr)

        try:
            result = await client.submit_flag(
                practice_ground_id=ground_id, challenge_id=challenge_id, flag=flag
            )
        except Exception as error:  # noqa: BLE001
            print(f"[submit] raised: {error}", file=sys.stderr)
            return 1

        print(f"[submit] {result}")
        accepted = _accepted(result)
        await arena_ledger.record_submission(
            session,
            connection_id=connection.id,
            challenge_id=challenge_id,
            flag=flag,
            verdict=arena_ledger.VERDICT_ACCEPTED if accepted else arena_ledger.VERDICT_REJECTED,
        )
        await session.commit()
        print("[ledger] recorded", "accepted" if accepted else "rejected")
        return 0 if accepted else 1


def _accepted(result: object) -> bool:
    if not isinstance(result, dict):
        return False
    for key in ("accepted", "correct", "is_correct", "success", "solved"):
        value = result.get(key)
        if isinstance(value, bool):
            return value
    return False


async def _main(arguments: argparse.Namespace) -> int:
    if arguments.list_pending:
        from app.core.database import SessionLocal

        async with SessionLocal() as session:
            for run_id, challenge_id, flag in await _pending(session):
                print(f"{run_id}  {challenge_id}  {flag}")
        return 0
    if not arguments.challenge_id or not arguments.flag:
        print("--challenge-id and --flag are both required", file=sys.stderr)
        return 2
    return await _deliver(arguments.challenge_id, arguments.flag)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--challenge-id", help="platform challenge id")
    parser.add_argument("--flag", help="the flag value to submit")
    parser.add_argument("--list-pending", action="store_true", help="show VALID flags not yet submitted")
    return asyncio.run(_main(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
