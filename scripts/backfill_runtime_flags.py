"""Backfill ``flag_candidates`` from Muteki run events.

Before ``flag_service.record_runtime_flag`` existed, the Muteki path published a
found flag only as a ``muteki.flag_found`` run event.  ``flag_candidates`` -- the
table the benchmark evaluator reads -- stayed empty, so a solved Run was scored
``INCOMPLETE`` with ``verified_flag_candidates: 0``.

This repairs existing Runs.  It re-verifies every value against the Challenge's
declared banner through the same service method the live path now uses, so a
backfilled row cannot be stronger than a fresh one.

Usage::

    python scripts/backfill_runtime_flags.py --dry-run
    python scripts/backfill_runtime_flags.py --run-id 9e095f0c-...
    python scripts/backfill_runtime_flags.py --since 2026-10-16
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models.challenge import Challenge
from app.models.run import FlagCandidate, RunEvent, SolveRun
from app.services.flags import flag_service

_FLAG_EVENT_TYPES = {"muteki.flag_found", "flag_found"}


def _flag_values(payload: dict) -> list[str]:
    values: list[str] = []
    for key in ("flag", "candidate"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    nested = payload.get("payload")
    if isinstance(nested, dict):
        values.extend(_flag_values(nested))
    return values


async def _collect(session, run_id: str) -> list[str]:
    events = list(
        (
            await session.scalars(
                select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.sequence)
            )
        ).all()
    )
    found: list[str] = []
    for event in events:
        if str(event.event_type) not in _FLAG_EVENT_TYPES:
            continue
        for value in _flag_values(event.payload_json or {}):
            if value not in found:
                found.append(value)
    return found


async def _run(args) -> int:
    async with SessionLocal() as session:
        query = select(SolveRun).order_by(SolveRun.created_at)
        if args.run_id:
            query = query.where(SolveRun.id == args.run_id)
        if args.since:
            query = query.where(SolveRun.created_at >= args.since)
        runs = list((await session.scalars(query)).all())

        touched = 0
        recorded = 0
        for run in runs:
            values = await _collect(session, str(run.id))
            if not values:
                continue
            challenge = await session.get(Challenge, run.challenge_id)
            if challenge is None:
                continue
            for value in values:
                existing = await session.scalar(
                    select(FlagCandidate).where(
                        FlagCandidate.run_id == run.id, FlagCandidate.candidate == value
                    )
                )
                if existing is not None and existing.verified:
                    continue
                touched += 1
                print(f"{run.id}  {run.status:22} {value}")
                if args.dry_run:
                    continue
                item = await flag_service.record_runtime_flag(session, run, challenge, value)
                if item is not None and item.verified:
                    recorded += 1
        print(
            json.dumps(
                {
                    "runs_scanned": len(runs),
                    "candidates_missing": touched,
                    "now_verified": recorded,
                    "dry_run": bool(args.dry_run),
                },
                ensure_ascii=False,
            )
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--since", type=lambda value: datetime.fromisoformat(value), default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
