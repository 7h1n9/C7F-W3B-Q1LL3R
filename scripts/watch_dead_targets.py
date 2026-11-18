"""Watch a batch as it runs and kill boxes whose target is not serving.

Why this exists
---------------
Batch 1 (2026-11-07) spent **52 of 130 minutes** on two boxes that were never
reachable: Confluence answered ``503 Service Unavailable`` and Nexus answered
``502 Bad Gateway`` for the whole 26-minute window.  Neither failure had
anything to do with the payload or the agent -- the target simply was not up.

The platform publishes no target URL until a lease is granted, so the only place
to check is *after* the Run starts: ``challenges.target_url`` is written from the
lease a few seconds in.  This watcher polls it, and when a Run has been going for
``--grace`` seconds and the target has answered 5xx (or refused) on every
attempt, it cancels the Run so the queue moves on to a box that might work.

Deliberately conservative:

* only 5xx / connection failure counts -- a 401 or 404 is a live target;
* a single good response clears the strikes, because a container that is still
  starting may 502 once and then come up;
* it cancels, never stops the queue.

Run it beside the queue, not instead of it::

    python scripts/watch_dead_targets.py --interval 20 --grace 90
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = "http://127.0.0.1:8000"


def _mysql(sql: str) -> list[list[str]]:
    """One read-only query, tab separated."""

    proc = subprocess.run(
        [
            "docker", "exec", "c7f-w3b-q1ll3r-mysql-1",
            "mysql", "-uctf_agent", "-pctf_agent", "ctf_agent",
            "-N", "-B", "-r", "-e", sql,
        ],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        return []
    return [line.split("\t") for line in proc.stdout.splitlines() if line.strip()]


def running_runs() -> list[dict]:
    rows = _mysql(
        """select r.id, c.target_url, c.name
             from solve_runs r join challenges c on c.id = r.challenge_id
            where r.status = 'RUNNING'"""
    )
    return [
        {"run_id": r[0], "target_url": (r[1] if len(r) > 1 else ""), "name": r[2] if len(r) > 2 else ""}
        for r in rows
    ]


def started_seconds_ago(run_id: str) -> float:
    rows = _mysql(
        f"select timestampdiff(second, started_at, now()) from solve_runs where id like '{run_id[:8]}%'"
    )
    try:
        return float(rows[0][0])
    except (IndexError, ValueError):
        return 0.0


#: A reply that means "the service behind the target is not up".  A 401, 403 or
#: 404 is a live application answering, so it must not count.
_DEAD_STATUS = {500, 502, 503, 504}


def probe(url: str) -> tuple[str, int | None]:
    """``("ok"|"dead"|"unknown", status)`` for one request."""

    if not url:
        return "unknown", None
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "mkt-watch/1"})
    # No proxy: the target is reached directly and the environment proxy would
    # turn every request into a 502 from the proxy itself.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            return "ok", response.status
    except urllib.error.HTTPError as exc:
        return ("dead" if exc.code in _DEAD_STATUS else "ok"), exc.code
    except Exception:
        return "dead", None


def cancel(run_id: str) -> bool:
    request = urllib.request.Request(
        f"{BACKEND}/api/v1/runs/{run_id}/cancel", method="POST", data=b"",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=20) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=20.0)
    parser.add_argument("--grace", type=float, default=90.0,
                        help="seconds a Run may be up before 5xx counts against it")
    parser.add_argument("--strikes", type=int, default=3,
                        help="consecutive dead probes before cancelling")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)

    strikes: dict[str, int] = {}
    while True:
        for run in running_runs():
            run_id = run["run_id"]
            age = started_seconds_ago(run_id)
            if age < args.grace:
                continue
            verdict, status = probe(run["target_url"])
            if verdict == "ok":
                if strikes.pop(run_id, None):
                    print(f"[{time.strftime('%H:%M:%S')}] {run['name'][:28]} recovered ({status})")
                continue
            if verdict == "unknown":
                continue
            count = strikes.get(run_id, 0) + 1
            strikes[run_id] = count
            print(f"[{time.strftime('%H:%M:%S')}] {run['name'][:28]} dead probe {count}/{args.strikes} (status={status}, age={age:.0f}s)")
            if count >= args.strikes:
                ok = cancel(run_id)
                print(f"    -> cancel {'sent' if ok else 'FAILED'}: target never came up")
                strikes.pop(run_id, None)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
