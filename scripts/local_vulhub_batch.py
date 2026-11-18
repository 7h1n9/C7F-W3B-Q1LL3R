#!/usr/bin/env python
"""Drive the local vulhub ranges as a repeatable solve batch.

The arena platform is an external dependency: when it refuses to boot an
environment (``ARENA_ENVIRONMENT_FAILED``) there is nothing to measure, and a
"the solver cannot do CVE-2018-12613" conclusion drawn in that state is wrong.
This script removes the platform from the loop.  It stands the same vulhub
reproductions up locally, registers each as a Challenge with the flag that the
range actually contains, and then creates real Muteki Runs against them through
the public API -- the same code path production uses, minus the platform.

Usage (from the repository root, backend on 127.0.0.1:8000)::

    python scripts/local_vulhub_batch.py setup
    python scripts/local_vulhub_batch.py run --names phpmyadmin
    python scripts/local_vulhub_batch.py status

``run`` prints the Run id; ``status`` polls the API and prints one line per Run
with the flag ledger state, so a batch result is readable without opening the UI.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.environ.get("C7F_BACKEND_URL", "http://127.0.0.1:8000")
FLAG_RE = re.compile(r"flag\{[0-9a-f]{32}\}")

#: The Workers used for the batch.  Both are CLI engines, because that is the
#: path that reads ``AGENTS.md`` -- where the curated known-exploit recipe is
#: seeded.  A chat Worker never sees that file, so a batch run through it would
#: measure something other than the fix under test.
WORKER_ENGINES = [
    {"engine_type": "claude_cli", "model_config_id": "c402b884-cffd-4a85-9b2b-e1bde65aff96"},
    {"engine_type": "codex_cli", "model_config_id": "7dc8b94a-802d-41b4-bb14-dfdd721a25ed"},
]
REASON_MODEL_CONFIG_ID = "daf2b404-43aa-4dc1-9d62-7a8344adbe2b"


@dataclass(frozen=True)
class Range:
    key: str
    name: str
    cve: str
    port: int
    dirname: str
    summary: str

    @property
    def flag_path(self) -> Path:
        return ROOT / "ranges" / self.dirname / "flag.txt"

    def flag(self) -> str:
        match = FLAG_RE.search(self.flag_path.read_text(encoding="utf-8"))
        if not match:
            raise SystemExit(f"{self.key}: {self.flag_path} does not contain a flag")
        return match.group(0)

    @property
    def target_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


RANGES: tuple[Range, ...] = (
    Range(
        key="phpmyadmin",
        name="[LOCAL][PHPMYADMIN]CVE-2018-12613",
        cve="CVE-2018-12613",
        port=8390,
        dirname="vulhub-phpmyadmin-cve-2018-12613",
        summary=(
            "A phpMyAdmin installation is served on the target and answers "
            "without authentication. A flag file exists at /flag.txt on the "
            "target filesystem and the web process can read it."
        ),
    ),
    Range(
        key="spring",
        name="[LOCAL][Spring]CVE-2016-4977",
        cve="CVE-2016-4977",
        port=8391,
        dirname="vulhub-spring-cve-2016-4977",
        summary=(
            "A Spring Security OAuth2 authorization server is served on the "
            "target. The only account is admin:admin. A flag file exists at "
            "/flag.txt on the target filesystem and the application can read it."
        ),
    ),
    Range(
        key="rails",
        name="[LOCAL][Rails]CVE-2018-3760",
        cve="CVE-2018-3760",
        port=8392,
        dirname="vulhub-rails-cve-2018-3760",
        summary=(
            "A Rails application in development mode is served on the target. "
            "A flag file exists at /flag on the target filesystem (no "
            "extension) and the application can read it."
        ),
    ),
    Range(
        key="tomcat",
        name="[LOCAL][Tomcat]CVE-2017-12615",
        cve="CVE-2017-12615",
        port=8384,
        dirname="vulhub-tomcat-cve-2017-12615",
        summary=(
            "The stock Apache Tomcat ROOT application is served on the target. "
            "A flag file exists at /flag.txt on the target filesystem and the "
            "web server process can read it."
        ),
    ),
    Range(
        key="weblogic2894",
        name="[LOCAL][WEBLOGIC]CVE-2018-2894",
        cve="CVE-2018-2894",
        port=8393,
        dirname="vulhub-weblogic-cve-2018-2894",
        summary=(
            "Oracle WebLogic Server 12.2.1.3 is served on the target in "
            "development mode. A flag file exists at /flag.txt on the target "
            "filesystem and the WebLogic process can read it."
        ),
    ),
)

BY_KEY = {item.key: item for item in RANGES}


def _request(method: str, path: str, payload: dict | None = None, timeout: float = 60.0) -> dict:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        BASE_URL + path,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        raise SystemExit(f"{method} {path} -> HTTP {error.code}: {detail[:500]}") from error
    return json.loads(raw) if raw else {}


def _challenges_by_name() -> dict[str, dict]:
    listing = _request("GET", "/api/v1/challenges?limit=500")
    items = listing.get("data") or []
    if isinstance(items, dict):
        items = items.get("items") or []
    return {item["name"]: item for item in items if isinstance(item, dict) and "name" in item}


def cmd_setup(args: argparse.Namespace) -> int:
    existing = _challenges_by_name()
    for spec in _selected(args.names):
        flag = spec.flag()
        payload = {
            "name": spec.name,
            "description": (
                f"Local reproduction of {spec.cve} "
                f"(https://github.com/vulhub/vulhub/tree/master).\n"
                f"{spec.summary}"
            ),
            "challenge_type": "WEB_TARGET",
            "target_url": spec.target_url,
            "allowed_hosts": ["127.0.0.1", f"127.0.0.1:{spec.port}"],
            "flag_pattern": r"flag\{[0-9a-f]{32}\}",
            "status": "ACTIVE",
            "metadata_json": {
                "local_range": True,
                "range_dir": spec.dirname,
                "cve": spec.cve,
                "initial_knowledge": [spec.summary],
            },
        }
        current = existing.get(spec.name)
        if current:
            _request("PUT", f"/api/v1/challenges/{current['id']}", payload)
            print(f"{spec.key:12s} updated  {current['id']}  flag={flag}")
        else:
            created = _request("POST", "/api/v1/challenges", payload)["data"]
            print(f"{spec.key:12s} created  {created['id']}  flag={flag}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    existing = _challenges_by_name()
    for spec in _selected(args.names):
        challenge = existing.get(spec.name)
        if not challenge:
            raise SystemExit(f"{spec.key}: challenge missing, run `setup` first")
        created = _request(
            "POST",
            f"/api/v1/challenges/{challenge['id']}/runs",
            {
                "engine_type": "claude_cli",
                "solver_mode": "muteki",
                "worker_engines": WORKER_ENGINES,
                "reason_model_config_id": REASON_MODEL_CONFIG_ID,
                "max_runtime_seconds": args.runtime,
                "max_total_runtime_seconds": args.total_runtime,
            },
        )["data"]
        # Creating a Run only persists it: the UI's "start" button is a separate
        # call, and a Run left in CREATED never touches a Worker.  Starting here
        # keeps `run` a one-shot command.
        _request("POST", f"/api/v1/runs/{created['id']}/start", {})
        print(f"{spec.key:12s} run={created['id']} status=STARTED target={spec.target_url}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    existing = _challenges_by_name()
    runs: list[tuple[str, dict]] = []
    for spec in _selected(args.names):
        challenge = existing.get(spec.name)
        if not challenge:
            continue
        listing = _request("GET", f"/api/v1/challenges/{challenge['id']}/runs?limit=5")
        items = listing.get("data") or []
        if isinstance(items, dict):
            items = items.get("items") or []
        for run in items[: args.per_challenge]:
            runs.append((spec.key, run))
    # Oldest first: a batch reads as a timeline.
    runs.sort(key=lambda pair: pair[1].get("created_at") or "")
    for key, run in runs:
        print(
            f"{key:12s} {run['id'][:8]}  {run['status']:24s} "
            f"phase={run.get('current_phase'):16s} title={str(run.get('title'))[:42]}"
        )
    return 0


def cmd_wait(args: argparse.Namespace) -> int:
    """Block until every selected Run is terminal, then print the ledger."""
    existing = _challenges_by_name()
    pending: dict[str, str] = {}
    for spec in _selected(args.names):
        challenge = existing.get(spec.name)
        if not challenge:
            continue
        listing = _request("GET", f"/api/v1/challenges/{challenge['id']}/runs?limit=1")
        items = listing.get("data") or []
        if isinstance(items, dict):
            items = items.get("items") or []
        if items:
            pending[items[0]["id"]] = spec.key
    if not pending:
        print("no runs to wait for")
        return 1
    deadline = time.monotonic() + args.timeout
    while pending and time.monotonic() < deadline:
        for run_id, key in list(pending.items()):
            run = _request("GET", f"/api/v1/runs/{run_id}")["data"]
            if run["status"].startswith("COMPLETED") or run["status"] in {"FAILED", "CANCELLED", "TIMEOUT"}:
                print(f"{key:12s} {run_id[:8]}  {run['status']}  {run.get('title') or ''}")
                del pending[run_id]
        if pending:
            time.sleep(args.interval)
    for run_id, key in pending.items():
        print(f"{key:12s} {run_id[:8]}  STILL RUNNING after {args.timeout}s")
    return 0 if not pending else 2


def _latest_run(challenge_id: str) -> dict | None:
    listing = _request("GET", f"/api/v1/challenges/{challenge_id}/runs?limit=1")
    items = listing.get("data") or []
    if isinstance(items, dict):
        items = items.get("items") or []
    return items[0] if items else None


def _flag_rows(run_id: str) -> str:
    listing = _request("GET", f"/api/v1/runs/{run_id}/flag-candidates")
    items = listing.get("data") or []
    if isinstance(items, dict):
        items = items.get("items") or []
    if not items:
        return "-"
    return ",".join(
        f"{item.get('candidate')}[{'VALID' if item.get('verified') else item.get('review_state') or 'OPEN'}]"
        for item in items
    )


MYSQL_CONTAINER = "c7f-w3b-q1ll3r-mysql-1"
MYSQL_ARGS = ["-uroot", "-pchange-me-before-use"]


def _mysql(sql: str) -> str:
    result = subprocess.run(
        ["docker", "exec", MYSQL_CONTAINER, "mysql", *MYSQL_ARGS, "-N", "-e", sql],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[:400])
    return result.stdout


def clear_blocking_idle_transactions(min_age_seconds: int = 60) -> list[int]:
    """Kill *idle* transactions that are actively blocking someone else.

    Every finished Run can leave an idle transaction behind on one pooled
    connection: no running statement, an open transaction, a handful of row
    locks.  The next Run's ``INSERT INTO run_execution_leases`` then waits on it,
    the lease retry loop burns all four attempts, and the Run sits in
    ``CREATED``/``INTAKE`` with no Worker at all.  Observed twice in one evening
    at 181 s and 230 s of age.

    The rule is deliberately narrow, because killing a blanket "idle > N"
    transaction would also kill the *current* Run's session (its supervisor
    session is legitimately idle while the sandbox does the work):

    * it must be named as a blocker in ``performance_schema.data_lock_waits``,
      i.e. something is already stuck behind it, and
    * it must have ``trx_query IS NULL`` and be older than ``min_age_seconds``,
      i.e. it is not doing anything that could still finish.

    Within a serial batch the only waiter can be the Run we just started, so a
    blocker that matches both conditions is a leaked transaction from a previous
    Run, never legitimate work.
    """

    rows = _mysql(
        "select distinct t.PROCESSLIST_ID "
        "from performance_schema.data_lock_waits w "
        "join performance_schema.threads t on t.THREAD_ID = w.BLOCKING_THREAD_ID "
        "join information_schema.innodb_trx x on x.trx_mysql_thread_id = t.PROCESSLIST_ID "
        "where x.trx_query is null "
        f"  and timestampdiff(second, x.trx_started, now()) > {min_age_seconds}"
    )
    victims = [int(line) for line in rows.split() if line.strip().isdigit()]
    for thread_id in victims:
        _mysql(f"kill {thread_id}")
    return victims


def cmd_all(args: argparse.Namespace) -> int:
    """Run every selected range one after another and print a result table.

    The backend owns a single Muteki control receiver (``MUTEKI_CONTROL_PORT``,
    default 9100) for the whole process, so two Runs that overlap bound their
    Workers to the same control endpoint and the second one misroutes its
    traffic.  Serialising here is not politeness -- concurrency is simply not
    supported by the runtime.
    """

    existing = _challenges_by_name()
    rows: list[tuple[str, str, str, str, str, str]] = []
    for spec in _selected(args.names):
        challenge = existing.get(spec.name)
        if not challenge:
            raise SystemExit(f"{spec.key}: challenge missing, run `setup` first")
        # The previous Run's leaked transaction is what blocks this one's lease,
        # so clear it before asking for the lease rather than after it stalls.
        leftover = clear_blocking_idle_transactions()
        if leftover:
            print(f"[{spec.key}] cleared blocking idle transactions {leftover}", flush=True)
        created = _request(
            "POST",
            f"/api/v1/challenges/{challenge['id']}/runs",
            {
                "engine_type": "claude_cli",
                "solver_mode": "muteki",
                "worker_engines": WORKER_ENGINES,
                "reason_model_config_id": REASON_MODEL_CONFIG_ID,
                "max_runtime_seconds": args.runtime,
                "max_total_runtime_seconds": args.total_runtime,
            },
        )["data"]
        run_id = created["id"]
        _request("POST", f"/api/v1/runs/{run_id}/start", {})
        print(f"[{spec.key}] started run={run_id}", flush=True)

        deadline = time.monotonic() + args.timeout
        status = "UNKNOWN"
        started_at_seen = False
        stalled_polls = 0
        while time.monotonic() < deadline:
            time.sleep(args.interval)
            run = _request("GET", f"/api/v1/runs/{run_id}")["data"]
            status = run["status"]
            if run.get("started_at"):
                started_at_seen = True
            elif status in {"CREATED", "INTAKE"} and not started_at_seen:
                # The lease is still blocked: this is exactly the symptom of a
                # leaked idle transaction from the previous Run.
                stalled_polls += 1
                if stalled_polls >= 2:
                    cleared = clear_blocking_idle_transactions(min_age_seconds=30)
                    if cleared:
                        print(f"[{spec.key}] unblocked lease, killed {cleared}", flush=True)
            if status.startswith("COMPLETED") or status in {"FAILED", "CANCELLED", "TIMEOUT"}:
                break
        expected = spec.flag()
        print(
            f"[{spec.key}] {status:22s} run={run_id[:8]} "
            f"flags={_flag_rows(run_id)} expected={expected}",
            flush=True,
        )
        rows.append((spec.key, run_id, status, _flag_rows(run_id), expected, ""))

    print()
    print(f"{'range':12s} {'run':10s} {'status':24s} flag-ledger")
    for key, run_id, status, flags, _expected, _unused in rows:
        print(f"{key:12s} {run_id[:8]:10s} {status:24s} {flags}")
    solved = sum(1 for row in rows if row[2] == "COMPLETED_SOLVED")
    print(f"\nsolved {solved}/{len(rows)}")
    return 0 if solved == len(rows) else 2


def _selected(names: list[str] | None) -> list[Range]:
    if not names:
        return list(RANGES)
    missing = [name for name in names if name not in BY_KEY]
    if missing:
        raise SystemExit(f"unknown range(s): {', '.join(missing)}")
    return [BY_KEY[name] for name in names]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--names", nargs="*", help="range keys (default: all)")
    sub = parser.add_subparsers(dest="command", required=True)

    setup = sub.add_parser("setup", help="create/update the Challenge rows")
    setup.add_argument("--names", nargs="*")
    setup.set_defaults(func=cmd_setup)

    run = sub.add_parser("run", help="create one Run per challenge")
    run.add_argument("--names", nargs="*")
    run.add_argument("--runtime", type=int, default=900)
    run.add_argument("--total-runtime", type=int, default=1200)
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="print recent Runs")
    status.add_argument("--names", nargs="*")
    status.add_argument("--per-challenge", type=int, default=2)
    status.set_defaults(func=cmd_status)

    wait = sub.add_parser("wait", help="block until Runs are terminal")
    wait.add_argument("--names", nargs="*")
    wait.add_argument("--timeout", type=int, default=2400)
    wait.add_argument("--interval", type=int, default=20)
    wait.set_defaults(func=cmd_wait)

    batch = sub.add_parser("all", help="run the whole batch serially and summarise")
    batch.add_argument("--names", nargs="*")
    batch.add_argument("--runtime", type=int, default=480)
    batch.add_argument("--total-runtime", type=int, default=600)
    batch.add_argument("--timeout", type=int, default=1800)
    batch.add_argument("--interval", type=int, default=15)
    batch.set_defaults(func=cmd_all)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
