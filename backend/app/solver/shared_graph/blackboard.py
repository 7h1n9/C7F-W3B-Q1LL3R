from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .bus import SolverEventBus
from .classification import ChallengeClassification, ClassificationFact
from .gate import FlagGate


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _loads(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


@dataclass(frozen=True, slots=True)
class Fact:
    id: str
    content: str
    source_worker_id: str
    verified: bool
    created_at: str
    fact_type: str = "OBSERVATION"
    value: Any = None
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DeadEnd:
    id: str
    description: str
    source_worker_id: str
    created_at: str


@dataclass(frozen=True, slots=True)
class Intent:
    id: str
    description: str
    status: str
    claimed_by: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class Flag:
    id: str
    flag_value: str
    source_worker_id: str
    verified_by_gate: bool
    created_at: str


@dataclass(frozen=True, slots=True)
class PoC:
    id: str
    content: str
    source_worker_id: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ResourceClaim:
    resource_id: str
    claimed_by: str | None
    claimed_at: str | None


class SharedGraph:
    """SQLite-backed collaboration graph for one challenge/run workspace.

    It is intentionally not the Solver Blackboard or the Evidence Store.  All
    writes require a worker identity, intent claiming is conditional, and the
    graph emits only compact references to the optional EventBus.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        worker_id: str | None = None,
        run_id: str = "",
        gate: FlagGate | None = None,
        event_bus: SolverEventBus | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.worker_id = worker_id
        self.run_id = run_id
        self.gate = gate or FlagGate()
        self.event_bus = event_bus
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS facts (
                    id TEXT PRIMARY KEY, content TEXT NOT NULL,
                    source_worker_id TEXT NOT NULL, verified INTEGER NOT NULL,
                    created_at TEXT NOT NULL, fact_type TEXT NOT NULL DEFAULT 'OBSERVATION',
                    value_json TEXT, evidence_refs_json TEXT
                );
                CREATE TABLE IF NOT EXISTS dead_ends (
                    id TEXT PRIMARY KEY, description TEXT NOT NULL,
                    source_worker_id TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS intents (
                    id TEXT PRIMARY KEY, description TEXT NOT NULL,
                    status TEXT NOT NULL, claimed_by TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS flags (
                    id TEXT PRIMARY KEY, flag_value TEXT NOT NULL,
                    source_worker_id TEXT NOT NULL, verified_by_gate INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pocs (
                    id TEXT PRIMARY KEY, content TEXT NOT NULL,
                    source_worker_id TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS resources (
                    resource_id TEXT PRIMARY KEY, claimed_by TEXT, claimed_at TEXT
                );
                """
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(facts)").fetchall()}
            if "fact_type" not in columns:
                db.execute("ALTER TABLE facts ADD COLUMN fact_type TEXT NOT NULL DEFAULT 'OBSERVATION'")
            if "value_json" not in columns:
                db.execute("ALTER TABLE facts ADD COLUMN value_json TEXT")
            if "evidence_refs_json" not in columns:
                db.execute("ALTER TABLE facts ADD COLUMN evidence_refs_json TEXT")

    def _worker(self, source_worker_id: str | None) -> str:
        value = source_worker_id or self.worker_id
        if not value:
            raise ValueError("source_worker_id is required for SharedGraph writes")
        return value

    def _emit(self, event_type: str, **payload: Any) -> None:
        if self.event_bus:
            self.event_bus.emit(event_type, run_id=self.run_id, payload=payload)

    def write_fact(
        self,
        content: str,
        verified: bool = False,
        *,
        source_worker_id: str | None = None,
        fact_type: str = "OBSERVATION",
        value: Any = None,
        evidence_refs: list[str] | tuple[str, ...] = (),
    ) -> str:
        worker = self._worker(source_worker_id)
        fact_id = f"fact_{uuid.uuid4().hex}"
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO facts(id, content, source_worker_id, verified, created_at, fact_type, value_json, evidence_refs_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (fact_id, str(content), worker, int(verified), _now(), str(fact_type), _json(value), _json(list(evidence_refs))),
            )
        self._emit("FACT_WRITTEN", fact_id=fact_id, source_worker_id=worker, verified=bool(verified), fact_type=str(fact_type))
        return fact_id

    def read_facts(self, limit: int = 50) -> list[Fact]:
        bounded = max(1, min(int(limit), 500))
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM facts ORDER BY created_at DESC LIMIT ?", (bounded,)
            ).fetchall()
        return [self._fact_from_row(row) for row in rows]

    @staticmethod
    def _fact_from_row(row: sqlite3.Row) -> Fact:
        return Fact(
            row["id"],
            row["content"],
            row["source_worker_id"],
            bool(row["verified"]),
            row["created_at"],
            row["fact_type"] or "OBSERVATION",
            _loads(row["value_json"]),
            tuple(_loads(row["evidence_refs_json"]) or ()),
        )

    def write_classification(
        self,
        classification: ChallengeClassification | str,
        *,
        confidence: int,
        evidence_refs: list[str] | tuple[str, ...] = (),
        source_worker_id: str | None = None,
    ) -> str:
        value = ChallengeClassification(str(classification).upper())
        score = int(confidence)
        if not 0 <= score <= 100:
            raise ValueError("classification confidence must be between 0 and 100")
        return self.write_fact(
            f"CHALLENGE_CLASSIFICATION={value.value}",
            verified=score >= 70,
            source_worker_id=source_worker_id,
            fact_type="CHALLENGE_CLASSIFICATION",
            value={"classification": value.value, "confidence": score},
            evidence_refs=evidence_refs,
        )

    def read_classification(self) -> ClassificationFact | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM facts WHERE fact_type='CHALLENGE_CLASSIFICATION' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        fact = self._fact_from_row(row)
        value = fact.value if isinstance(fact.value, dict) else {}
        try:
            classification = ChallengeClassification(str(value["classification"]).upper())
            confidence = int(value["confidence"])
        except (KeyError, TypeError, ValueError):
            return None
        return ClassificationFact(classification, confidence, fact.evidence_refs, fact.id, fact.source_worker_id)

    def mark_deadend(self, description: str, *, source_worker_id: str | None = None) -> str:
        worker = self._worker(source_worker_id)
        deadend_id = f"deadend_{uuid.uuid4().hex}"
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO dead_ends VALUES (?, ?, ?, ?)", (deadend_id, str(description), worker, _now()))
        self._emit("DEADEND_MARKED", deadend_id=deadend_id, source_worker_id=worker)
        return deadend_id

    def read_deadends(self) -> list[DeadEnd]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM dead_ends ORDER BY created_at DESC").fetchall()
        return [DeadEnd(row["id"], row["description"], row["source_worker_id"], row["created_at"]) for row in rows]

    def write_poc(self, content: str, *, source_worker_id: str | None = None) -> str:
        worker = self._worker(source_worker_id)
        poc_id = f"poc_{uuid.uuid4().hex}"
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO pocs VALUES (?, ?, ?, ?)", (poc_id, str(content), worker, _now()))
        self._emit("POC_WRITTEN", poc_id=poc_id, source_worker_id=worker)
        return poc_id

    def read_pocs(self) -> list[PoC]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM pocs ORDER BY created_at DESC").fetchall()
        return [PoC(row["id"], row["content"], row["source_worker_id"], row["created_at"]) for row in rows]

    def propose_intent(self, description: str, *, source_worker_id: str | None = None) -> str:
        worker = self._worker(source_worker_id)
        intent_id = f"intent_{uuid.uuid4().hex}"
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO intents VALUES (?, ?, 'proposed', NULL, ?)", (intent_id, str(description), _now()))
        self._emit("INTENT_PROPOSED", intent_id=intent_id, source_worker_id=worker)
        return intent_id

    def claim_intent(self, intent_id: str, worker_id: str | None = None) -> str:
        worker = self._worker(worker_id)
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE intents SET status='claimed', claimed_by=? WHERE id=? AND status='proposed'",
                (worker, intent_id),
            )
        result = "WON" if cursor.rowcount == 1 else "LOST"
        self._emit("INTENT_CLAIMED", intent_id=intent_id, worker_id=worker, result=result)
        return result

    def complete_intent(self, intent_id: str, *, source_worker_id: str | None = None) -> bool:
        worker = self._worker(source_worker_id)
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE intents SET status='done' WHERE id=? AND status='claimed' AND claimed_by=?",
                (intent_id, worker),
            )
        if cursor.rowcount:
            self._emit("INTENT_DONE", intent_id=intent_id, worker_id=worker)
        return cursor.rowcount == 1

    def list_intents(self, status: str = "proposed") -> list[Intent]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM intents WHERE status=? ORDER BY created_at ASC", (status,)
            ).fetchall()
        return [Intent(row["id"], row["description"], row["status"], row["claimed_by"], row["created_at"]) for row in rows]

    def read_flags(self, *, verified_only: bool = False) -> list[Flag]:
        query = "SELECT * FROM flags"
        params: tuple[object, ...] = ()
        if verified_only:
            query += " WHERE verified_by_gate=1"
        query += " ORDER BY created_at DESC"
        with self._connect() as db:
            rows = db.execute(query, params).fetchall()
        return [Flag(row["id"], row["flag_value"], row["source_worker_id"], bool(row["verified_by_gate"]), row["created_at"]) for row in rows]

    def write_flag(
        self,
        flag_value: str,
        *,
        worker_output: str,
        source_worker_id: str | None = None,
    ) -> str:
        worker = self._worker(source_worker_id)
        decision = self.gate.verify(flag_value, worker_output=worker_output)
        flag_id = f"flag_{uuid.uuid4().hex}"
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO flags VALUES (?, ?, ?, ?, ?)",
                (flag_id, decision.flag_value, worker, int(decision.accepted), _now()),
            )
        event_type = "FLAG_FOUND" if decision.accepted else "FLAG_CANDIDATE"
        self._emit(event_type, flag_id=flag_id, source_worker_id=worker, reason_code=decision.reason_code)
        return flag_id

    def claim_resource(self, resource_id: str, worker_id: str | None = None) -> str:
        worker = self._worker(worker_id)
        now = _now()
        with self._lock, self._connect() as db:
            db.execute("INSERT OR IGNORE INTO resources(resource_id) VALUES (?)", (resource_id,))
            cursor = db.execute(
                "UPDATE resources SET claimed_by=?, claimed_at=? WHERE resource_id=? AND claimed_by IS NULL",
                (worker, now, resource_id),
            )
        return "WON" if cursor.rowcount == 1 else "LOST"

    def release_resource(self, resource_id: str, worker_id: str | None = None) -> bool:
        worker = self._worker(worker_id)
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE resources SET claimed_by=NULL, claimed_at=NULL WHERE resource_id=? AND claimed_by=?",
                (resource_id, worker),
            )
        return cursor.rowcount == 1

    def revision(self) -> int:
        with self._connect() as db:
            counts = [db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("facts", "dead_ends", "intents", "flags", "pocs", "resources")]
        return sum(int(count) for count in counts)

    def snapshot(self, *, limit: int = 50) -> dict[str, Any]:
        return {
            "revision": self.revision(),
            "facts": [self._fact_dict(fact) for fact in self.read_facts(limit)],
            "challenge_classification": self._classification_dict(self.read_classification()),
            "deadends": [{"id": item.id, "description": item.description, "source_worker_id": item.source_worker_id, "created_at": item.created_at} for item in self.read_deadends()],
            "flags": [{"id": item.id, "flag_value": item.flag_value, "source_worker_id": item.source_worker_id, "verified_by_gate": item.verified_by_gate, "created_at": item.created_at} for item in self.read_flags()],
            "pocs": [{"id": item.id, "content": item.content, "source_worker_id": item.source_worker_id, "created_at": item.created_at} for item in self.read_pocs()],
            "intents": [{"id": item.id, "description": item.description, "status": item.status, "claimed_by": item.claimed_by, "created_at": item.created_at} for item in self.list_intents("proposed")],
        }

    @staticmethod
    def _fact_dict(fact: Fact) -> dict[str, Any]:
        return {"id": fact.id, "content": fact.content, "source_worker_id": fact.source_worker_id, "verified": fact.verified, "created_at": fact.created_at, "fact_type": fact.fact_type, "value": fact.value, "evidence_refs": list(fact.evidence_refs)}

    @staticmethod
    def _classification_dict(value: ClassificationFact | None) -> dict[str, Any] | None:
        if value is None:
            return None
        return {"classification": value.classification.value, "confidence": value.confidence, "evidence_refs": list(value.evidence_refs), "fact_id": value.fact_id, "source_worker_id": value.source_worker_id}
