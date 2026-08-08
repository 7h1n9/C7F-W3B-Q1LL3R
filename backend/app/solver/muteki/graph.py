from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .events import EventEnvelope, EventType
from .gate import MutekiFlagGate


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


@dataclass(frozen=True, slots=True)
class Fact:
    fact_id: int
    content: str
    source_worker_id: str
    verified: bool
    created_at: str
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DeadEnd:
    dead_end_id: int
    description: str
    source_worker_id: str
    created_at: str


@dataclass(frozen=True, slots=True)
class Intent:
    intent_id: str
    description: str
    status: str
    claimed_by: str | None
    created_at: str
    lease_until: float | None = None
    payload: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Flag:
    flag_id: int
    flag_value: str
    source_worker_id: str
    verified_by_gate: bool
    created_at: str


@dataclass(frozen=True, slots=True)
class PoC:
    poc_id: str
    content: str
    source_worker_id: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ResourceClaim:
    resource_id: str
    claimed_by: str | None
    lease_until: float | None


class MutekiGraph:
    """Append-only event-sourced graph with SQLite materialized projections."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        challenge_id: str,
        event_subscriber: Callable[[EventEnvelope], None] | None = None,
        gate: MutekiFlagGate | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.challenge_id = challenge_id
        self.gate = gate or MutekiFlagGate()
        self._subscriber = event_subscriber
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA busy_timeout=10000")
        self._initialize()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def emit_event(self, *, actor: str, event_type: EventType | str, payload: dict[str, Any] | None = None) -> int:
        with self._lock:
            event = self._append(actor, event_type, dict(payload or {}))
            self._db.commit()
            return event.sequence

    def release_claims(self, *, actor: str) -> None:
        with self._lock:
            self._db.execute("UPDATE intents SET status='open', claimed_by=NULL, lease_until=NULL WHERE status='claimed'")
            self._db.execute("UPDATE resources SET claimed_by=NULL, lease_until=NULL WHERE claimed_by IS NOT NULL")
            self._db.commit()

    def rebuild_projections(self) -> None:
        """Rebuild query tables from the immutable event log."""
        with self._lock:
            self._db.executescript("DELETE FROM facts; DELETE FROM dead_ends; DELETE FROM intents; DELETE FROM flags;")
            rows = self._db.execute("SELECT * FROM events ORDER BY sequence").fetchall()
            for row in rows:
                payload = _load(row["payload_json"], {})
                kind = row["event_type"]
                if kind == EventType.FACT_ADDED:
                    self._db.execute("INSERT OR IGNORE INTO facts VALUES (?, ?, ?, ?, ?, ?)", (row["sequence"], payload.get("content", ""), row["actor"], int(row["verified"]), row["timestamp"], _dump(payload.get("evidence_refs", []))))
                elif kind == EventType.DEAD_END:
                    self._db.execute("INSERT OR IGNORE INTO dead_ends VALUES (?, ?, ?, ?)", (row["sequence"], payload.get("description", ""), row["actor"], row["timestamp"]))
                elif kind == EventType.INTENT_PROPOSED:
                    self._db.execute("INSERT OR IGNORE INTO intents VALUES (?, ?, 'open', NULL, NULL, ?, ?)", (payload.get("intent_id", ""), payload.get("description", ""), row["timestamp"], _dump(payload.get("payload", {}))))
                elif kind == EventType.INTENT_CLAIMED:
                    self._db.execute("UPDATE intents SET status='claimed', claimed_by=?, lease_until=? WHERE intent_id=?", (row["actor"], payload.get("lease_until"), payload.get("intent_id")))
                elif kind == EventType.INTENT_CONCLUDED:
                    self._db.execute("UPDATE intents SET status='done', lease_until=NULL WHERE intent_id=?", (payload.get("intent_id"),))
                elif kind in {EventType.FLAG_FOUND, EventType.FLAG_CANDIDATE}:
                    self._db.execute("INSERT OR IGNORE INTO flags VALUES (?, ?, ?, ?, ?)", (row["sequence"], payload.get("flag", ""), row["actor"], int(kind == EventType.FLAG_FOUND), row["timestamp"]))
            self._db.commit()

    def _initialize(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                challenge_id TEXT NOT NULL,
                actor TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                verified INTEGER NOT NULL DEFAULT 0,
                confidence REAL NOT NULL DEFAULT 1.0,
                dedupe_key TEXT UNIQUE
            );
            CREATE TABLE IF NOT EXISTS facts (
                fact_id INTEGER PRIMARY KEY,
                content TEXT NOT NULL,
                source_worker_id TEXT NOT NULL,
                verified INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                evidence_refs_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dead_ends (
                dead_end_id INTEGER PRIMARY KEY,
                description TEXT NOT NULL,
                source_worker_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS intents (
                intent_id TEXT PRIMARY KEY,
                description TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                claimed_by TEXT,
                lease_until REAL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS flags (
                flag_id INTEGER PRIMARY KEY,
                flag_value TEXT NOT NULL,
                source_worker_id TEXT NOT NULL,
                verified_by_gate INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pocs (
                poc_id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                source_worker_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS resources (
                resource_id TEXT PRIMARY KEY,
                claimed_by TEXT,
                lease_until REAL
            );
            """
        )
        columns = {str(row[1]) for row in self._db.execute("PRAGMA table_info(intents)").fetchall()}
        if "payload_json" not in columns:
            self._db.execute("ALTER TABLE intents ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}'" )
        self._db.commit()

    def _append(
        self,
        actor: str,
        event_type: EventType | str,
        payload: dict[str, Any],
        *,
        verified: bool = False,
        confidence: float = 1.0,
        dedupe_key: str | None = None,
    ) -> EventEnvelope:
        event = EventEnvelope.now(
            0,
            challenge_id=self.challenge_id,
            actor=actor,
            event_type=event_type,
            payload=payload,
            verified=verified,
            confidence=confidence,
        )
        cursor = self._db.execute(
            "INSERT INTO events(timestamp, challenge_id, actor, event_type, payload_json, verified, confidence, dedupe_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (event.timestamp, self.challenge_id, actor, str(event_type), _dump(payload), int(verified), confidence, dedupe_key),
        )
        sequence = int(cursor.lastrowid)
        event = EventEnvelope(sequence, event.timestamp, self.challenge_id, actor, str(event_type), dict(payload), verified, confidence)
        if self._subscriber:
            self._subscriber(event)
        return event

    def add_fact(
        self,
        *,
        actor: str,
        content: str,
        verified: bool = False,
        evidence_refs: list[str] | tuple[str, ...] = (),
        dedupe_key: str | None = None,
    ) -> int:
        with self._lock:
            try:
                event = self._append(actor, EventType.FACT_ADDED, {"content": content, "evidence_refs": list(evidence_refs)}, verified=verified, dedupe_key=dedupe_key)
            except sqlite3.IntegrityError:
                row = self._db.execute("SELECT sequence FROM events WHERE dedupe_key=?", (dedupe_key,)).fetchone()
                if row is None:
                    raise
                return int(row["sequence"])
            self._db.execute(
                "INSERT INTO facts(fact_id, content, source_worker_id, verified, created_at, evidence_refs_json) VALUES (?, ?, ?, ?, ?, ?)",
                (event.sequence, content, actor, int(verified), event.timestamp, _dump(list(evidence_refs))),
            )
            self._db.commit()
            return event.sequence

    def add_dead_end(self, *, actor: str, description: str) -> int:
        with self._lock:
            event = self._append(actor, EventType.DEAD_END, {"description": description})
            self._db.execute("INSERT INTO dead_ends VALUES (?, ?, ?, ?)", (event.sequence, description, actor, event.timestamp))
            self._db.commit()
            return event.sequence

    def propose_intent(self, *, actor: str, intent_id: str | None = None, description: str, payload: dict[str, Any] | None = None) -> str:
        intent_id = intent_id or f"intent_{uuid.uuid4().hex}"
        with self._lock:
            event = self._append(actor, EventType.INTENT_PROPOSED, {"intent_id": intent_id, "description": description, "payload": payload or {}})
            self._db.execute("INSERT INTO intents VALUES (?, ?, 'open', NULL, NULL, ?, ?)", (intent_id, description, event.timestamp, _dump(payload or {})))
            self._db.commit()
        return intent_id

    def claim_intent(self, *, worker: str, intent_id: str, lease_s: float = 300.0) -> bool:
        lease_until = datetime.now(UTC).timestamp() + max(1.0, float(lease_s))
        with self._lock:
            cursor = self._db.execute(
                "UPDATE intents SET status='claimed', claimed_by=?, lease_until=? WHERE intent_id=? AND status='open' AND (lease_until IS NULL OR lease_until<?)",
                (worker, lease_until, intent_id, datetime.now(UTC).timestamp()),
            )
            if cursor.rowcount != 1:
                self._db.rollback()
                return False
            self._append(worker, EventType.INTENT_CLAIMED, {"intent_id": intent_id, "lease_until": lease_until})
            self._db.commit()
            return True

    def conclude_intent(self, *, actor: str, intent_id: str, result: str = "") -> bool:
        with self._lock:
            cursor = self._db.execute("UPDATE intents SET status='done', lease_until=NULL WHERE intent_id=? AND status='claimed'", (intent_id,))
            if cursor.rowcount != 1:
                self._db.rollback()
                return False
            self._append(actor, EventType.INTENT_CONCLUDED, {"intent_id": intent_id, "result": result})
            self._db.commit()
            return True

    def write_flag(self, *, actor: str, flag: str, real_output: str) -> int:
        decision = self.gate.verify(flag, real_output=real_output)
        event_type = EventType.FLAG_FOUND if decision.accepted else EventType.FLAG_CANDIDATE
        with self._lock:
            event = self._append(actor, event_type, {"flag": decision.flag, "reason": decision.reason}, verified=decision.accepted)
            self._db.execute("INSERT INTO flags VALUES (?, ?, ?, ?, ?)", (event.sequence, decision.flag, actor, int(decision.accepted), event.timestamp))
            self._db.commit()
            return event.sequence

    def save_poc(self, *, actor: str, poc_id: str, content: str) -> str:
        with self._lock:
            event = self._append(actor, EventType.POC_SAVED, {"poc_id": poc_id, "content": content})
            self._db.execute("INSERT OR REPLACE INTO pocs VALUES (?, ?, ?, ?)", (poc_id, content, actor, event.timestamp))
            self._db.commit()
        return poc_id

    def claim_resource(self, *, worker: str, resource_id: str, lease_s: float = 600.0) -> bool:
        until = datetime.now(UTC).timestamp() + max(1.0, float(lease_s))
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO resources(resource_id) VALUES (?)", (resource_id,))
            cursor = self._db.execute("UPDATE resources SET claimed_by=?, lease_until=? WHERE resource_id=? AND (claimed_by IS NULL OR lease_until<?)", (worker, until, resource_id, datetime.now(UTC).timestamp()))
            if cursor.rowcount != 1:
                self._db.rollback()
                return False
            self._append(worker, EventType.RESOURCE_LOCKED, {"resource_id": resource_id, "lease_until": until})
            self._db.commit()
            return True

    def release_resource(self, *, worker: str, resource_id: str) -> bool:
        with self._lock:
            cursor = self._db.execute("UPDATE resources SET claimed_by=NULL, lease_until=NULL WHERE resource_id=? AND claimed_by=?", (resource_id, worker))
            if cursor.rowcount != 1:
                self._db.rollback()
                return False
            self._append(worker, EventType.RESOURCE_RELEASED, {"resource_id": resource_id})
            self._db.commit()
            return True

    def facts(self, *, verified_only: bool = False) -> list[Fact]:
        query = "SELECT * FROM facts"
        if verified_only:
            query += " WHERE verified=1"
        query += " ORDER BY fact_id"
        rows = self._db.execute(query).fetchall()
        return [Fact(row["fact_id"], row["content"], row["source_worker_id"], bool(row["verified"]), row["created_at"], tuple(_load(row["evidence_refs_json"], []))) for row in rows]

    def dead_ends(self) -> list[DeadEnd]:
        rows = self._db.execute("SELECT * FROM dead_ends ORDER BY dead_end_id").fetchall()
        return [DeadEnd(row["dead_end_id"], row["description"], row["source_worker_id"], row["created_at"]) for row in rows]

    def intents(self, *, status: str | None = None) -> list[Intent]:
        query = "SELECT * FROM intents"
        params: tuple[Any, ...] = ()
        if status:
            query += " WHERE status=?"
            params = (status,)
        rows = self._db.execute(query + " ORDER BY created_at", params).fetchall()
        return [Intent(row["intent_id"], row["description"], row["status"], row["claimed_by"], row["created_at"], row["lease_until"], _load(row["payload_json"], {})) for row in rows]

    def flags(self, *, verified_only: bool = False) -> list[Flag]:
        query = "SELECT * FROM flags"
        if verified_only:
            query += " WHERE verified_by_gate=1"
        rows = self._db.execute(query + " ORDER BY flag_id").fetchall()
        return [Flag(row["flag_id"], row["flag_value"], row["source_worker_id"], bool(row["verified_by_gate"]), row["created_at"]) for row in rows]

    def events_since(self, after: int = 0) -> list[EventEnvelope]:
        rows = self._db.execute("SELECT * FROM events WHERE sequence>? ORDER BY sequence", (int(after),)).fetchall()
        return [EventEnvelope(row["sequence"], row["timestamp"], row["challenge_id"], row["actor"], row["event_type"], _load(row["payload_json"], {}), bool(row["verified"]), float(row["confidence"])) for row in rows]

    def revision(self) -> int:
        row = self._db.execute("SELECT COALESCE(MAX(sequence), 0) AS value FROM events").fetchone()
        return int(row["value"])

    def snapshot(self) -> dict[str, Any]:
        return {
            "challenge_id": self.challenge_id,
            "revision": self.revision(),
            "facts": [fact.__dict__ if hasattr(fact, "__dict__") else {"fact_id": fact.fact_id, "content": fact.content, "source_worker_id": fact.source_worker_id, "verified": fact.verified, "created_at": fact.created_at, "evidence_refs": list(fact.evidence_refs)} for fact in self.facts()],
            "dead_ends": [{"dead_end_id": item.dead_end_id, "description": item.description, "source_worker_id": item.source_worker_id} for item in self.dead_ends()],
            "intents": [{"intent_id": item.intent_id, "description": item.description, "status": item.status, "claimed_by": item.claimed_by} for item in self.intents()],
            "flags": [{"flag_id": item.flag_id, "flag_value": item.flag_value, "verified_by_gate": item.verified_by_gate} for item in self.flags()],
        }
