#!/usr/bin/env python3
"""stdlib-only Muteki worker skill.

This file is intentionally usable after being copied into a worker image. It
does not import FastAPI, SQLAlchemy, the Coordinator, or the application
package. The only worker-to-coordinator channel is the shared SQLite file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path


def _db_path() -> Path:
    value = os.environ.get("MUTEKI_BLACKBOARD_DB", "")
    if not value:
        print("ERROR: MUTEKI_BLACKBOARD_DB is required", file=sys.stderr)
        raise SystemExit(2)
    return Path(value)


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=10000")
    return db


def _init(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL, challenge_id TEXT NOT NULL,
            actor TEXT NOT NULL, event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL, verified INTEGER NOT NULL DEFAULT 0,
            confidence REAL NOT NULL DEFAULT 1.0, dedupe_key TEXT UNIQUE
        );
        CREATE TABLE IF NOT EXISTS facts (
            fact_id INTEGER PRIMARY KEY, content TEXT NOT NULL,
            source_worker_id TEXT NOT NULL, verified INTEGER NOT NULL,
            created_at TEXT NOT NULL, evidence_refs_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS dead_ends (
            dead_end_id INTEGER PRIMARY KEY, description TEXT NOT NULL,
            source_worker_id TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS intents (
            intent_id TEXT PRIMARY KEY, description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open', claimed_by TEXT,
            lease_until REAL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS flags (
            flag_id INTEGER PRIMARY KEY, flag_value TEXT NOT NULL,
            source_worker_id TEXT NOT NULL, verified_by_gate INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pocs (
            poc_id TEXT PRIMARY KEY, content TEXT NOT NULL,
            source_worker_id TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS resources (
            resource_id TEXT PRIMARY KEY, claimed_by TEXT,
            lease_until REAL
        );
        """
    )
    # The application graph keeps an intent payload column for coordinator
    # routing.  Add it when this stdlib-only skill is pointed at an older
    # standalone blackboard, while remaining compatible with an existing
    # canonical graph database.
    columns = {str(row[1]) for row in db.execute("PRAGMA table_info(intents)").fetchall()}
    if "payload_json" not in columns:
        db.execute("ALTER TABLE intents ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}'")


def _actor() -> str:
    return os.environ.get("MUTEKI_WORKER_ID", "worker")


def _challenge() -> str:
    return os.environ.get("MUTEKI_CHALLENGE_ID", "challenge")


def _append(db: sqlite3.Connection, event_type: str, payload: dict, *, verified: bool = False) -> int:
    cursor = db.execute(
        "INSERT INTO events(timestamp, challenge_id, actor, event_type, payload_json, verified) VALUES (?, ?, ?, ?, ?, ?)",
        (datetime.now(UTC).isoformat(), _challenge(), _actor(), event_type, json.dumps(payload, ensure_ascii=False), int(verified)),
    )
    return int(cursor.lastrowid)


def _write_fact(db: sqlite3.Connection, content: str, verified: bool, evidence_refs: list[str]) -> int:
    sequence = _append(db, "fact_added", {"content": content, "evidence_refs": evidence_refs}, verified=verified)
    db.execute("INSERT INTO facts VALUES (?, ?, ?, ?, ?, ?)", (sequence, content, _actor(), int(verified), datetime.now(UTC).isoformat(), json.dumps(evidence_refs)))
    db.commit()
    return sequence


def _flag_ok(flag: str, output: str) -> tuple[bool, str]:
    if not re.fullmatch(r"flag\{[^{}\r\n]+\}", flag):
        return False, "FORMAT_INVALID"
    if flag.casefold() in {"flag{test}", "flag{placeholder}", "flag{dummy}"}:
        return False, "PLACEHOLDER"
    if flag not in output:
        return False, "NOT_IN_REAL_OUTPUT"
    return True, "ACCEPTED"


def main() -> int:
    parser = argparse.ArgumentParser(description="Muteki worker blackboard skill")
    sub = parser.add_subparsers(dest="command", required=True)
    write_fact = sub.add_parser("write-fact")
    write_fact.add_argument("content")
    write_fact.add_argument("--verified", action="store_true")
    write_fact.add_argument("--evidence-ref", action="append", default=[])
    read_facts = sub.add_parser("read-facts")
    read_facts.add_argument("--verified-only", action="store_true")
    dead_end = sub.add_parser("mark-deadend")
    dead_end.add_argument("description")
    propose = sub.add_parser("propose-intent")
    propose.add_argument("description")
    list_intents = sub.add_parser("list-intents")
    list_intents.add_argument("--status", default=None)
    claim = sub.add_parser("claim")
    claim.add_argument("intent_id")
    sub.add_parser("read-resource-locks")
    claim_resource = sub.add_parser("claim-resource")
    claim_resource.add_argument("resource_id")
    claim_resource.add_argument("--risk-class", default="exclusive")
    release_resource = sub.add_parser("release-resource")
    release_resource.add_argument("resource_id")
    write_poc = sub.add_parser("write-poc")
    write_poc.add_argument("poc_id")
    write_poc.add_argument("content")
    read_pocs = sub.add_parser("read-pocs")
    read_pocs.add_argument("--category", default=None)
    read_poc = sub.add_parser("read-poc")
    read_poc.add_argument("poc_id")
    flag = sub.add_parser("write-flag")
    flag.add_argument("flag")
    flag.add_argument("--real-output", required=True)
    args = parser.parse_args()
    with _connect() as db:
        _init(db)
        if args.command == "write-fact":
            result = _write_fact(db, args.content, args.verified, args.evidence_ref)
        elif args.command == "read-facts":
            query = "SELECT * FROM facts"
            if args.verified_only:
                query += " WHERE verified=1"
            result = [dict(row) for row in db.execute(query + " ORDER BY fact_id").fetchall()]
        elif args.command == "mark-deadend":
            sequence = _append(db, "dead_end", {"description": args.description})
            db.execute("INSERT INTO dead_ends VALUES (?, ?, ?, ?)", (sequence, args.description, _actor(), datetime.now(UTC).isoformat()))
            db.commit()
            result = sequence
        elif args.command == "propose-intent":
            intent_id = f"intent_{uuid.uuid4().hex}"
            _append(db, "intent_proposed", {"intent_id": intent_id, "description": args.description})
            db.execute(
                "INSERT INTO intents(intent_id, description, status, claimed_by, lease_until, created_at, payload_json) VALUES (?, ?, 'open', NULL, NULL, ?, '{}')",
                (intent_id, args.description, datetime.now(UTC).isoformat()),
            )
            db.commit()
            result = intent_id
        elif args.command == "list-intents":
            query = "SELECT * FROM intents"
            params: tuple[str, ...] = ()
            if args.status:
                query += " WHERE status=?"
                params = (args.status,)
            result = [dict(row) for row in db.execute(query + " ORDER BY created_at", params).fetchall()]
        elif args.command == "claim":
            until = datetime.now(UTC).timestamp() + 300
            cursor = db.execute("UPDATE intents SET status='claimed', claimed_by=?, lease_until=? WHERE intent_id=? AND status='open' AND (lease_until IS NULL OR lease_until<?)", (_actor(), until, args.intent_id, datetime.now(UTC).timestamp()))
            if cursor.rowcount:
                _append(db, "intent_claimed", {"intent_id": args.intent_id, "lease_until": until})
                db.commit()
                result = "WON"
            else:
                db.rollback()
                result = "LOST"
        elif args.command == "read-resource-locks":
            now = datetime.now(UTC).timestamp()
            result = [dict(row) for row in db.execute("SELECT * FROM resources WHERE claimed_by IS NOT NULL AND (lease_until IS NULL OR lease_until>?) ORDER BY resource_id", (now,)).fetchall()]
        elif args.command == "claim-resource":
            until = datetime.now(UTC).timestamp() + 600
            db.execute("INSERT OR IGNORE INTO resources(resource_id) VALUES (?)", (args.resource_id,))
            cursor = db.execute("UPDATE resources SET claimed_by=?, lease_until=? WHERE resource_id=? AND (claimed_by IS NULL OR lease_until<?)", (_actor(), until, args.resource_id, datetime.now(UTC).timestamp()))
            if cursor.rowcount:
                _append(db, "resource_locked", {"resource_id": args.resource_id, "risk_class": args.risk_class, "lease_until": until})
                db.commit()
                result = "WON"
            else:
                db.rollback()
                result = "LOST"
        elif args.command == "release-resource":
            cursor = db.execute("UPDATE resources SET claimed_by=NULL, lease_until=NULL WHERE resource_id=? AND claimed_by=?", (args.resource_id, _actor()))
            if cursor.rowcount:
                _append(db, "resource_released", {"resource_id": args.resource_id})
                db.commit()
                result = "RELEASED"
            else:
                db.rollback()
                result = "NOT_OWNER"
        elif args.command == "write-poc":
            _append(db, "poc_saved", {"poc_id": args.poc_id, "content": args.content})
            db.execute("INSERT OR REPLACE INTO pocs VALUES (?, ?, ?, ?)", (args.poc_id, args.content, _actor(), datetime.now(UTC).isoformat()))
            db.commit()
            result = args.poc_id
        elif args.command == "read-pocs":
            rows = db.execute("SELECT * FROM pocs ORDER BY created_at").fetchall()
            result = [dict(row) for row in rows if not args.category or args.category.casefold() in str(row["content"]).casefold()]
        elif args.command == "read-poc":
            row = db.execute("SELECT * FROM pocs WHERE poc_id=?", (args.poc_id,)).fetchone()
            result = dict(row) if row else None
        else:
            accepted, reason = _flag_ok(args.flag, args.real_output)
            sequence = _append(db, "flag_found" if accepted else "flag_candidate", {"flag": args.flag, "reason": reason}, verified=accepted)
            db.execute("INSERT INTO flags VALUES (?, ?, ?, ?, ?)", (sequence, args.flag, _actor(), int(accepted), datetime.now(UTC).isoformat()))
            db.commit()
            result = sequence
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
