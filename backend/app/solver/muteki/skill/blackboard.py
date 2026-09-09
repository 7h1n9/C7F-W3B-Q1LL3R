#!/usr/bin/env python3
"""muteki-blackboard - a worker's CLI to the shared solve graph (the blackboard).

A swarm worker (claude / codex / openai) calls this to coordinate with its
teammates through the shared, append-only SQLite blackboard - NOT by talking to
them directly (stigmergy). The board holds:

  - facts      : confirmed, objective findings (with verified/candidate status)
  - dead-ends  : ruled-out directions (so nobody retries them)
  - intents    : declared exploration directions, claimable atomically
  - routes     : review-arbiter suppressed/reopened routes
  - branches   : forked hypotheses to prove/disprove separately
  - activities : in-progress high-cost work (nmap, brute force, etc.)
  - resources  : exclusive locks (ports, accounts, listeners)
  - directives : operator / coordinator guidance that must be respected

The DB path comes from MUTEKI_BLACKBOARD_DB (the coordinator sets it per
worker). This script is intentionally dependency-free (stdlib sqlite3 only) so
it runs in any worker container without setup.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import UTC, datetime

_ACTOR = os.environ.get("MUTEKI_WORKER_ID", "worker")
_INTENT_ID = os.environ.get("MUTEKI_INTENT_ID", "").strip()


def _db_path() -> str:
    p = os.environ.get("MUTEKI_BLACKBOARD_DB", "")
    if not p:
        for cand in (".muteki_blackboard", "shared_graph.db"):
            if os.path.isfile(cand):
                return cand
        print("ERROR: no blackboard DB (MUTEKI_BLACKBOARD_DB unset and no "
              "shared_graph.db in cwd)", file=sys.stderr)
        sys.exit(2)
    return p


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_db_path(), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=5000")
    return c


def _has_column(c: sqlite3.Connection, table: str, col: str) -> bool:
    try:
        cols = {row[1] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return False
    return col in cols


def _has_table(c: sqlite3.Connection, table: str) -> bool:
    try:
        row = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    except Exception:
        return False
    return row is not None

def _retired_fact_seqs(c: sqlite3.Connection) -> set:
    """fact_seqs in a terminal lifecycle state (rejected/merged/superseded)."""
    if not _has_table(c, "fact_states"):
        return set()
    try:
        rows = c.execute(
            "SELECT fact_seq FROM fact_states "
            "WHERE state IN ('rejected','merged','superseded') OR retired_seq IS NOT NULL"
        ).fetchall()
    except Exception:
        return set()
    return {int(r[0]) for r in rows}


def _challenge_id(c: sqlite3.Connection) -> str:
    env = os.environ.get("MUTEKI_CHALLENGE_ID", "").strip()
    if env:
        return env
    row = c.execute(
        "SELECT challenge_id FROM events "
        "WHERE challenge_id IS NOT NULL AND challenge_id != '' LIMIT 1"
    ).fetchone()
    if row and row[0]:
        return row[0]
    try:
        row = c.execute(
            "SELECT challenge_id FROM intents "
            "WHERE challenge_id IS NOT NULL AND challenge_id != '' LIMIT 1"
        ).fetchone()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    return ""


def _read_events(c: sqlite3.Connection, kind: str) -> list[sqlite3.Row]:
    try:
        return c.execute(
            "SELECT sequence, timestamp, challenge_id, actor, event_type, payload_json, verified, confidence "
            "FROM events WHERE event_type=? ORDER BY sequence",
            (kind,),
        ).fetchall()
    except Exception:
        return []


def read_facts(verified_only: bool) -> None:
    c = _conn()
    retired = _retired_fact_seqs(c)
    q = ("SELECT sequence, payload_json, verified, confidence FROM events "
         "WHERE event_type='fact_added' ORDER BY sequence")
    out = []
    for seq, payload, verified, conf in c.execute(q).fetchall():
        if int(seq) in retired:
            continue
        if verified_only and not verified:
            continue
        d = json.loads(payload or "{}")
        out.append({"fact": d.get("content", "") or d.get("fact", ""),
                    "source": d.get("source", "") or "",
                    "verified": bool(verified), "confidence": conf})
    if not out:
        print("(no facts on the board yet)")
        return
    for f in out:
        tag = "VERIFIED" if f["verified"] else f"candidate({f['confidence']:.1f})"
        print(f"[{tag}] ({f['source']}) {f['fact']}")


def read_flags() -> None:
    c = _conn()
    rows = c.execute(
        "SELECT payload_json, event_type FROM events "
        "WHERE event_type IN ('flag_found','flag_candidate','flag_invalidated') ORDER BY sequence"
    ).fetchall()
    found: list[str] = []
    for payload, kind in rows:
        d = (json.loads(payload or "{}") or {})
        f = d.get("flag") or d.get("flag_value") or ""
        if not f:
            continue
        if kind == "flag_found" and f not in found:
            found.append(f)
        elif kind == "flag_invalidated" and f in found:
            found.remove(f)
    if not found:
        print("(no flags recovered yet - you may be the first)")
        return
    print("# Flags already recovered by the team - do NOT re-submit these:")
    for f in found:
        print(f"- {f}")


def read_deadends() -> None:
    c = _conn()
    rows = c.execute(
        "SELECT payload_json FROM events WHERE event_type='dead_end' ORDER BY sequence"
    ).fetchall()
    if not rows:
        print("(no dead-ends recorded - nothing ruled out yet)")
        return
    print("# Dead-ends - directions already ruled out, DO NOT retry these:")
    for (payload,) in rows:
        d = json.loads(payload or "{}")
        print(f"- {d.get('description', '') or d.get('reason', '')}")


def read_routes() -> None:
    c = _conn()
    if not _has_table(c, "routes"):
        print("(this board has no route review table yet)")
        return
    rows = c.execute(
        "SELECT route_hash, label, status, reason, until_policy "
        "FROM routes ORDER BY COALESCE(suppressed_seq, reopened_seq, 0), route_hash"
    ).fetchall()
    if not rows:
        print("(no reviewed routes)")
        return
    print("# Reviewed routes")
    for route_hash, label, status, reason, until_policy in rows:
        tag = "SUPPRESSED" if status == "suppressed" else "OPEN"
        extra = f" until={until_policy}" if until_policy else ""
        print(f"[{tag}] {route_hash} ({label or route_hash}){extra}: {reason or ''}")


def read_branches() -> None:
    c = _conn()
    if not _has_table(c, "branches"):
        print("(this board has no branch review table yet)")
        return
    rows = c.execute(
        "SELECT branch_id, parent_id, title, assumption, prove_or_disprove, status "
        "FROM branches ORDER BY created_seq, branch_id"
    ).fetchall()
    if not rows:
        print("(no branch hypotheses)")
        return
    print("# Review branches - prove/disprove separately")
    for branch_id, parent_id, title, assumption, pod, status in rows:
        parent = f" parent={parent_id}" if parent_id else ""
        print(f"- [{status or 'open'}] {branch_id}{parent}: {title or assumption}")
        if assumption:
            print(f"  assumption: {assumption}")
        if pod:
            print(f"  prove/disprove: {pod}")


def read_review() -> None:
    c = _conn()
    print("# Review-Arbiter state")
    rows = c.execute(
        "SELECT sequence, actor, payload_json FROM events "
        "WHERE event_type='review_finding' ORDER BY sequence DESC LIMIT 12"
    ).fetchall()
    if rows:
        print("\n## Findings")
        for seq, actor, payload in reversed(rows):
            d = json.loads(payload or "{}")
            sev = d.get("severity", "info")
            kind = d.get("kind", "finding")
            route = f" route={d.get('route_hash')}" if d.get("route_hash") else ""
            print(f"- #{seq} [{sev}/{kind}] {actor}:{route} {d.get('summary', '')}")

    challenged: list[tuple] = []
    if _has_table(c, "fact_reviews"):
        challenged = c.execute(
            "SELECT fact_seq, status, reason, verification_intent_id "
            "FROM fact_reviews WHERE status='challenged' ORDER BY challenged_seq"
        ).fetchall()
    if challenged:
        print("\n## Challenged facts - do NOT rely on these until verified")
        for fact_seq, status, reason, verification_intent_id in challenged:
            fact = ""
            try:
                row = c.execute(
                    "SELECT payload_json FROM events WHERE sequence=?", (int(fact_seq),)
                ).fetchone()
                if row:
                    fact = (json.loads(row["payload_json"] or "{}") or {}).get("content", "")
            except Exception:
                pass
            print(f"- fact #{fact_seq}: {fact}")
            print(f"  reason: {reason or ''}")
            if verification_intent_id:
                print(f"  verify intent: {verification_intent_id}")

    dirs = c.execute(
        "SELECT sequence, actor, payload_json FROM events "
        "WHERE event_type='coordinator_directive' ORDER BY sequence DESC LIMIT 8"
    ).fetchall()
    if dirs:
        print("\n## Coordinator directives")
        for seq, actor, payload in reversed(dirs):
            d = json.loads(payload or "{}")
            print(f"- #{seq} {actor} {d.get('action', 'note')}: {d.get('directive', '')}")

    print("\n## Routes")
    read_routes()
    print("\n## Branches")
    read_branches()


def list_intents() -> None:
    c = _conn()
    if not _has_table(c, "intents"):
        print("(no open intents)")
        return
    cols = {row[1] for row in c.execute("PRAGMA table_info(intents)").fetchall()}
    where = "status='open'"
    if "dispatch_state" in cols:
        where += " AND dispatch_state='active'"
    rows = c.execute(
        "SELECT intent_id, description, worker_class, route_hash, branch_id FROM intents WHERE " + where + " ORDER BY created_at"
    ).fetchall() if "worker_class" in cols else c.execute(
        "SELECT intent_id, description FROM intents WHERE " + where + " ORDER BY created_at"
    ).fetchall()
    if not rows:
        print("(no open intents)")
        return
    print("# Open intents you can claim:")
    for row in rows:
        iid = row[0]
        goal = row[1]
        meta = []
        if len(row) > 2 and row[2]:
            meta.append(f"class={row[2]}")
        if len(row) > 3 and row[3]:
            meta.append(f"route={row[3]}")
        if len(row) > 4 and row[4]:
            meta.append(f"branch={row[4]}")
        suffix = f" [{' '.join(meta)}]" if meta else ""
        print(f"- {iid}: {goal}{suffix}")

def _normalize_fact_identity(text: str, actor: str) -> str:
    """Dedupe on fact IDENTITY, matching MutekiGraph.add_fact semantics."""
    norm = re.sub(r"^\[[a-z0-9 _.-]{1,40}\]\s*", "", text, flags=re.IGNORECASE)
    norm = " ".join(norm.split()).lower()
    return f"fact::{actor}::{norm}"


def write_fact(text: str, verified: bool, evidence_refs: list[str] | None = None) -> None:
    c = _conn()
    cid = _challenge_id(c)
    payload = {
        "source": _ACTOR, "content": text, "source_solver": _ACTOR,
        "witness": None, "verifier": _ACTOR if verified else "",
        "evidence_refs": list(evidence_refs or []),
    }
    if _INTENT_ID:
        payload["intent_id"] = _INTENT_ID
    dk = _normalize_fact_identity(text, _ACTOR)
    try:
        cur = c.execute(
            "INSERT INTO events (timestamp, challenge_id, actor, event_type, payload_json, "
            "verified, confidence, dedupe_key) VALUES (?,?,?,?,?,?,?,?)",
            (datetime.now(UTC).isoformat(), cid, _ACTOR, "fact_added",
             json.dumps(payload, ensure_ascii=False), int(verified),
             1.0 if verified else 0.4, dk))
        fact_seq = int(cur.lastrowid or 0)
        try:
            c.execute(
                "INSERT INTO facts(fact_id, content, source_worker_id, verified, created_at, evidence_refs_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (fact_seq, text, _ACTOR, int(verified),
                 datetime.now(UTC).isoformat(),
                 json.dumps(list(evidence_refs or []), ensure_ascii=False)))
        except Exception:
            pass
        if _INTENT_ID and fact_seq > 0 and _has_table(c, "intent_products"):
            c.execute(
                "INSERT OR IGNORE INTO intent_products (intent_id, fact_seq) VALUES (?,?)",
                (_INTENT_ID, fact_seq))
        c.commit()
        print(f"OK wrote {'verified' if verified else 'candidate'} fact")
    except sqlite3.IntegrityError:
        print("OK (duplicate fact, already on board)")


def mark_deadend(reason: str) -> None:
    c = _conn()
    cid = _challenge_id(c)
    payload = json.dumps({"description": reason, "reason": reason})
    try:
        cur = c.execute(
            "INSERT INTO events (timestamp, challenge_id, actor, event_type, payload_json, "
            "verified, confidence, dedupe_key) VALUES (?,?,?,?,?,?,?,?)",
            (datetime.now(UTC).isoformat(), cid, _ACTOR, "dead_end", payload,
             0, 1.0, f"deadend::{reason}"))
        seq = int(cur.lastrowid or 0)
        try:
            c.execute(
                "INSERT INTO dead_ends(dead_end_id, description, source_worker_id, created_at) VALUES (?,?,?,?)",
                (seq, reason, _ACTOR, datetime.now(UTC).isoformat()))
        except Exception:
            pass
        c.commit()
        print("OK marked dead-end")
    except sqlite3.IntegrityError:
        print("OK (dead-end already recorded)")


def claim(intent_id: str) -> None:
    c = _conn()
    cid = _challenge_id(c)
    now = datetime.now(UTC).timestamp()
    active_fence = " AND dispatch_state='active'" if _has_column(c, "intents", "dispatch_state") else ""
    challenge_fence = " AND challenge_id=?" if _has_column(c, "intents", "challenge_id") else ""
    params: list = [_ACTOR, now + 300.0, intent_id]
    if challenge_fence:
        params.append(cid)
    params.append(now)
    cur = c.execute(
        "UPDATE intents SET claimed_by=?, status='claimed', lease_until=? "
        "WHERE intent_id=?" + challenge_fence + active_fence +
        "  AND (status='open' OR (status='claimed' AND lease_until < ?))",
        tuple(params))
    c.commit()
    if cur.rowcount == 1:
        c.execute(
            "INSERT INTO events (timestamp, challenge_id, actor, event_type, payload_json, "
            "verified, confidence) VALUES (?,?,?,?,?,?,?)",
            (datetime.now(UTC).isoformat(), cid, _ACTOR, "intent_claimed",
             json.dumps({"intent_id": intent_id}), 0, 1.0))
        c.commit()
        print("WON")
    else:
        print("LOST")


def claim_activity(key: str, lease_s: float = 600.0) -> None:
    c = _conn()
    cid = _challenge_id(c)
    nkey = re.sub(r"[\s/]+", ":", (key or "").strip().lower())
    nkey = re.sub(r":+", ":", nkey).strip(":")
    now = datetime.now(UTC).timestamp()
    if not nkey:
        print("WON")
        return
    c.execute(
        "CREATE TABLE IF NOT EXISTS activity_locks ("
        "activity_key TEXT PRIMARY KEY, challenge_id TEXT NOT NULL, "
        "worker TEXT NOT NULL, lease_until REAL NOT NULL, claimed_ts REAL NOT NULL)")
    cur = c.execute(
        "INSERT INTO activity_locks "
        "(activity_key, challenge_id, worker, lease_until, claimed_ts) "
        "VALUES (?,?,?,?,?) "
        "ON CONFLICT(activity_key) DO UPDATE SET "
        "  worker=excluded.worker, lease_until=excluded.lease_until, "
        "  claimed_ts=excluded.claimed_ts "
        "WHERE activity_locks.lease_until < ?",
        (nkey, cid, _ACTOR, now + lease_s, now, now))
    c.commit()
    print("WON" if cur.rowcount == 1 else "LOST")


def list_activities() -> None:
    c = _conn()
    cid = _challenge_id(c)
    now = datetime.now(UTC).timestamp()
    try:
        rows = c.execute(
            "SELECT activity_key, worker FROM activity_locks "
            "WHERE challenge_id=? AND lease_until > ? ORDER BY claimed_ts",
            (cid, now)).fetchall()
    except Exception:
        rows = []
    if not rows:
        print("(no activities in progress)")
        return
    print("# Activities in progress (don't duplicate):")
    for key, worker in rows:
        print(f"{key}  [{worker}]")

def _normalize_resource_key(key: str) -> str:
    raw = re.sub(r"\s+", "", (key or "").strip().lower())
    raw = re.sub(r"[^a-z0-9_:@.*/-]+", "-", raw).strip("-")
    return raw[:180]


def claim_resource(resource_key: str, scope: str = "activity",
                   risk_class: str = "", lease_s: float = 600.0) -> None:
    c = _conn()
    cid = _challenge_id(c)
    rkey = _normalize_resource_key(resource_key)
    now = datetime.now(UTC).timestamp()
    if not rkey:
        print("WON")
        return
    c.execute(
        "CREATE TABLE IF NOT EXISTS resource_locks ("
        "lock_id TEXT PRIMARY KEY, challenge_id TEXT NOT NULL, resource_key TEXT NOT NULL, "
        "scope TEXT NOT NULL, risk_class TEXT, status TEXT NOT NULL DEFAULT 'requested', "
        "owner_worker TEXT, owner_intent TEXT, lease_until REAL, created_seq INTEGER, "
        "released_seq INTEGER, conflict_policy TEXT NOT NULL DEFAULT 'exclusive', "
        "cooldown_s REAL NOT NULL DEFAULT 0)")
    lock_id = f"rl-{rkey}"
    cur = c.execute(
        "INSERT INTO resource_locks "
        "(lock_id, challenge_id, resource_key, scope, risk_class, status, owner_worker, lease_until) "
        "VALUES (?,?,?,?,?,'active',?,?) "
        "ON CONFLICT(lock_id) DO UPDATE SET "
        "  status='active', owner_worker=excluded.owner_worker, "
        "  scope=excluded.scope, risk_class=excluded.risk_class, lease_until=excluded.lease_until "
        "WHERE resource_locks.owner_worker=excluded.owner_worker "
        "   OR resource_locks.lease_until IS NULL OR resource_locks.lease_until < ?",
        (lock_id, cid, rkey, scope or "activity", risk_class or None, _ACTOR,
         now + lease_s, now))
    c.commit()
    if cur.rowcount == 1:
        try:
            c.execute(
                "INSERT INTO events (timestamp, challenge_id, actor, event_type, payload_json, verified, confidence) "
                "VALUES (?,?,?,?,?,?,?)",
                (datetime.now(UTC).isoformat(), cid, _ACTOR, "resource_locked",
                 json.dumps({"resource_key": rkey, "scope": scope, "lock_id": lock_id}), 0, 1.0))
            c.commit()
        except Exception:
            pass
        print("WON")
    else:
        print("LOST")


def release_resource(resource_key: str) -> None:
    c = _conn()
    cid = _challenge_id(c)
    rkey = _normalize_resource_key(resource_key)
    now = datetime.now(UTC).timestamp()
    if not _has_table(c, "resource_locks") or not rkey:
        print("OK")
        return
    cur = c.execute(
        "UPDATE resource_locks SET status='released', owner_worker=NULL, lease_until=NULL "
        "WHERE challenge_id=? AND resource_key=? AND owner_worker=?",
        (cid, rkey, _ACTOR))
    c.commit()
    if cur.rowcount >= 1:
        try:
            c.execute(
                "INSERT INTO events (timestamp, challenge_id, actor, event_type, payload_json, verified, confidence) "
                "VALUES (?,?,?,?,?,?,?)",
                (datetime.now(UTC).isoformat(), cid, _ACTOR, "resource_released",
                 json.dumps({"resource_key": rkey}), 0, 1.0))
            c.commit()
        except Exception:
            pass
    print("OK")


def read_resource_locks() -> None:
    c = _conn()
    cid = _challenge_id(c)
    now = datetime.now(UTC).timestamp()
    if not _has_table(c, "resource_locks"):
        print("(no resource locks)")
        return
    rows = c.execute(
        "SELECT resource_key, scope, risk_class, owner_worker FROM resource_locks "
        "WHERE challenge_id=? AND status='active' AND owner_worker IS NOT NULL "
        "AND (lease_until IS NULL OR lease_until > ?) ORDER BY created_seq",
        (cid, now)).fetchall()
    if not rows:
        print("(no resource locks held)")
        return
    print("# Resource locks held by teammates (do NOT duplicate):")
    for rkey, scope, risk, owner in rows:
        risk_s = f" risk={risk}" if risk else ""
        print(f"- {rkey} (scope={scope}{risk_s}) [{owner}]")


def read_directives() -> None:
    c = _conn()
    cid = _challenge_id(c)
    if not _has_table(c, "operator_directives"):
        print("(no operator directives)")
        return
    rows = c.execute(
        "SELECT directive_id, action, text, status, priority FROM operator_directives "
        "WHERE challenge_id=? AND status NOT IN ('superseded','expired','rejected') "
        "ORDER BY priority DESC, received_seq",
        (cid,)).fetchall()
    if not rows:
        print("(no active operator directives)")
        return
    print("# Operator directives (must respect - guidance, not evidence):")
    for did, action, text, status, priority in rows:
        print(f"- [{action}/{status}] {text}  (id={did})")


def directive_status(directive_id: str) -> None:
    c = _conn()
    cid = _challenge_id(c)
    if not _has_table(c, "operator_directives"):
        print("(unknown)")
        return
    row = c.execute(
        "SELECT action, text, status, bound_worker FROM operator_directives "
        "WHERE challenge_id=? AND directive_id=?",
        (cid, directive_id)).fetchone()
    if not row:
        print("(unknown directive)")
        return
    action, text, status, bound = row
    bound_s = f" bound={bound}" if bound else ""
    print(f"{directive_id}: {action} status={status}{bound_s} :: {text}")

def write_poc(poc_id: str, content: str) -> None:
    c = _conn()
    cid = _challenge_id(c)
    c.execute(
        "INSERT INTO events (timestamp, challenge_id, actor, event_type, payload_json, verified, confidence) "
        "VALUES (?,?,?,?,?,?,?)",
        (datetime.now(UTC).isoformat(), cid, _ACTOR, "poc_saved",
         json.dumps({"poc_id": poc_id, "content": content}), 0, 1.0))
    c.execute("CREATE TABLE IF NOT EXISTS pocs (poc_id TEXT PRIMARY KEY, content TEXT NOT NULL, source_worker_id TEXT NOT NULL, created_at TEXT NOT NULL)")
    c.execute("INSERT OR REPLACE INTO pocs VALUES (?,?,?,?)",
              (poc_id, content, _ACTOR, datetime.now(UTC).isoformat()))
    c.commit()
    print(f"OK saved poc {poc_id}")


def read_pocs() -> None:
    c = _conn()
    if not _has_table(c, "pocs"):
        print("(no pocs saved)")
        return
    rows = c.execute("SELECT poc_id, source_worker_id, created_at FROM pocs ORDER BY created_at").fetchall()
    if not rows:
        print("(no pocs saved)")
        return
    print("# Saved PoCs")
    for poc_id, source, created in rows:
        print(f"- {poc_id} [{source}] ({created})")


def read_poc(poc_id: str) -> None:
    c = _conn()
    if not _has_table(c, "pocs"):
        print("(unknown poc)")
        return
    row = c.execute("SELECT content FROM pocs WHERE poc_id=?", (poc_id,)).fetchone()
    if not row:
        print("(unknown poc)")
        return
    print(row["content"])


def write_flag(flag: str, real_output: str) -> None:
    c = _conn()
    cid = _challenge_id(c)
    if not re.fullmatch(r"flag\{[^{}\r\n]+\}", flag):
        print("REJECTED: FORMAT_INVALID")
        return
    if flag.casefold() in {"flag{test}", "flag{placeholder}", "flag{dummy}"}:
        print("REJECTED: PLACEHOLDER")
        return
    if flag not in real_output:
        print("REJECTED: NOT_IN_REAL_OUTPUT")
        return
    payload = json.dumps({"flag": flag, "reason": "ACCEPTED"})
    c.execute(
        "INSERT INTO events (timestamp, challenge_id, actor, event_type, payload_json, verified, confidence) "
        "VALUES (?,?,?,?,?,?,?)",
        (datetime.now(UTC).isoformat(), cid, _ACTOR, "flag_found", payload, 1, 1.0))
    seq = int(c.lastrowid or 0)
    c.execute("CREATE TABLE IF NOT EXISTS flags (flag_id INTEGER PRIMARY KEY, flag_value TEXT NOT NULL, source_worker_id TEXT NOT NULL, verified_by_gate INTEGER NOT NULL, created_at TEXT NOT NULL)")
    c.execute("INSERT OR REPLACE INTO flags VALUES (?,?,?,?,?)",
              (seq, flag, _ACTOR, 1, datetime.now(UTC).isoformat()))
    c.commit()
    print(f"ACCEPTED flag {flag}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="blackboard.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("read-facts")
    p.add_argument("--verified-only", action="store_true")
    sub.add_parser("read-review")
    sub.add_parser("read-routes")
    sub.add_parser("read-branches")
    sub.add_parser("read-deadends")
    sub.add_parser("read-flags")
    sub.add_parser("list-intents")
    p = sub.add_parser("write-fact")
    p.add_argument("text")
    p.add_argument("--verified", action="store_true")
    p.add_argument("--evidence-ref", action="append", default=[])
    p = sub.add_parser("mark-deadend")
    p.add_argument("reason")
    p = sub.add_parser("claim")
    p.add_argument("intent_id")
    p = sub.add_parser("claim-activity")
    p.add_argument("key")
    sub.add_parser("list-activities")
    p = sub.add_parser("claim-resource")
    p.add_argument("resource_key")
    p.add_argument("--scope", default="activity")
    p.add_argument("--risk-class", default="")
    p = sub.add_parser("release-resource")
    p.add_argument("resource_key")
    sub.add_parser("read-resource-locks")
    sub.add_parser("read-directives")
    p = sub.add_parser("directive-status")
    p.add_argument("directive_id")
    p = sub.add_parser("write-poc")
    p.add_argument("poc_id")
    p.add_argument("content")
    sub.add_parser("read-pocs")
    p = sub.add_parser("read-poc")
    p.add_argument("poc_id")
    p = sub.add_parser("write-flag")
    p.add_argument("flag")
    p.add_argument("--real-output", required=True)
    args = ap.parse_args()

    if args.cmd == "read-facts":
        read_facts(args.verified_only)
    elif args.cmd == "read-review":
        read_review()
    elif args.cmd == "read-routes":
        read_routes()
    elif args.cmd == "read-branches":
        read_branches()
    elif args.cmd == "read-deadends":
        read_deadends()
    elif args.cmd == "read-flags":
        read_flags()
    elif args.cmd == "list-intents":
        list_intents()
    elif args.cmd == "write-fact":
        write_fact(args.text, args.verified, args.evidence_ref)
    elif args.cmd == "mark-deadend":
        mark_deadend(args.reason)
    elif args.cmd == "claim":
        claim(args.intent_id)
    elif args.cmd == "claim-activity":
        claim_activity(args.key)
    elif args.cmd == "list-activities":
        list_activities()
    elif args.cmd == "claim-resource":
        claim_resource(args.resource_key, scope=args.scope, risk_class=args.risk_class)
    elif args.cmd == "release-resource":
        release_resource(args.resource_key)
    elif args.cmd == "read-resource-locks":
        read_resource_locks()
    elif args.cmd == "read-directives":
        read_directives()
    elif args.cmd == "directive-status":
        directive_status(args.directive_id)
    elif args.cmd == "write-poc":
        write_poc(args.poc_id, args.content)
    elif args.cmd == "read-pocs":
        read_pocs()
    elif args.cmd == "read-poc":
        read_poc(args.poc_id)
    elif args.cmd == "write-flag":
        write_flag(args.flag, args.real_output)


if __name__ == "__main__":
    main()

