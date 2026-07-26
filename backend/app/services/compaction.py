"""Incremental, archive-before-delete compaction for active Runs.

The service intentionally has a deterministic fallback decision.  A model may
add a richer evidence snapshot, but model availability must never be allowed
to make an over-budget Run grow without limit.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.inspection import inspect as sa_inspect

from app.core.exceptions import DomainError
from app.models.run import (
    Artifact,
    EvidenceSnapshot,
    FlagCandidate,
    LogicalToolCall,
    Observation,
    RunCompactionCheckpoint,
    RunEvent,
    SolveRun,
    ToolCall,
    ToolExecutionTrace,
)
from app.models.solver_state import SolverState
from app.schemas.compaction import CompactionDecisionAction, empty_evidence_snapshot
from app.services.events import event_service


class CompactionService:
    # Soft trigger, hard trigger, and emergency trigger are intentionally
    # separate.  The hard trigger is deterministic and does not require an AI
    # review response.
    DEFAULT_THRESHOLD = 20
    SOFT_EVENT_THRESHOLD = 500
    HARD_EVENT_THRESHOLD = 1500
    MAX_PAYLOAD_BYTES = 5 * 1024 * 1024
    HARD_PAYLOAD_BYTES = 12 * 1024 * 1024
    EMERGENCY_PAYLOAD_BYTES = 25 * 1024 * 1024
    MAX_DELETE_BATCH = 200
    RECENT_TAIL_LOGICAL_CALLS = 10
    RECENT_TAIL_EVENTS = 50

    _archive_sets = {
        "run-events.jsonl.gz": RunEvent,
        "tool-calls.jsonl.gz": ToolCall,
        "observations.jsonl.gz": Observation,
        "traces.jsonl.gz": ToolExecutionTrace,
    }

    @staticmethod
    def _json_row(row) -> dict:
        return {attr.key: getattr(row, attr.key) for attr in sa_inspect(row).mapper.column_attrs}

    @staticmethod
    def _after(column, value):
        return column > value if value is not None else True

    async def metrics(self, session, run: SolveRun) -> dict:
        logical_total = int(
            await session.scalar(
                select(func.count(func.distinct(LogicalToolCall.id))).where(LogicalToolCall.run_id == run.id)
                .where(LogicalToolCall.counts_toward_budget.is_(True))
            )
            or 0
        )
        event_filter = [RunEvent.run_id == run.id]
        if int(run.last_compacted_event_id or 0):
            event_filter.append(
                or_(RunEvent.event_id > int(run.last_compacted_event_id), RunEvent.event_id.is_(None))
            )
        events = int(await session.scalar(select(func.count()).select_from(RunEvent).where(*event_filter)) or 0)
        payload_bytes = int(
            await session.scalar(
                select(func.coalesce(func.sum(RunEvent.payload_size), 0))
                .where(*event_filter)
            )
            or 0
        )
        observation_filter = [Observation.run_id == run.id]
        if run.last_compacted_observation_created_at:
            observation_filter.append(Observation.created_at > run.last_compacted_observation_created_at)
        artifact_filter = [Artifact.run_id == run.id]
        if run.last_compacted_artifact_created_at:
            artifact_filter.append(Artifact.created_at > run.last_compacted_artifact_created_at)
        trace_filter = [LogicalToolCall.run_id == run.id]
        if run.last_compacted_trace_created_at:
            trace_filter.append(ToolExecutionTrace.created_at > run.last_compacted_trace_created_at)
        observations = int(await session.scalar(select(func.count()).select_from(Observation).where(*observation_filter)) or 0)
        artifacts = int(await session.scalar(select(func.count()).select_from(Artifact).where(*artifact_filter)) or 0)
        traces = int(
            await session.scalar(
                select(func.count())
                .select_from(ToolExecutionTrace)
                .join(LogicalToolCall, ToolExecutionTrace.logical_tool_call_id == LogicalToolCall.id)
                .where(*trace_filter)
            )
            or 0
        )
        return {
            "effective_logical_calls_since_last_compaction": max(
                0, logical_total - int(run.last_compaction_effective_tool_count or 0)
            ),
            "events_since_last_compaction": events,
            "observations_since_last_compaction": observations,
            "artifacts_since_last_compaction": artifacts,
            "traces_since_last_compaction": traces,
            "payload_bytes_since_last_compaction": payload_bytes,
        }

    async def should_compact(self, session, run: SolveRun) -> tuple[bool, dict]:
        metrics = await self.metrics(session, run)
        soft = (
            metrics["effective_logical_calls_since_last_compaction"] >= self.DEFAULT_THRESHOLD
            or metrics["events_since_last_compaction"] >= self.SOFT_EVENT_THRESHOLD
            or metrics["payload_bytes_since_last_compaction"] >= self.MAX_PAYLOAD_BYTES
        )
        hard = (
            metrics["effective_logical_calls_since_last_compaction"] >= 40
            or metrics["events_since_last_compaction"] >= self.HARD_EVENT_THRESHOLD
            or metrics["payload_bytes_since_last_compaction"] >= self.HARD_PAYLOAD_BYTES
        )
        emergency = metrics["payload_bytes_since_last_compaction"] >= self.EMERGENCY_PAYLOAD_BYTES
        metrics.update({"soft_triggered": soft, "hard_triggered": hard, "emergency": emergency})
        return soft, metrics

    async def safe_point(self, session, run: SolveRun) -> tuple[bool, str | None]:
        status = str(run.status or "")
        if status.startswith(("PAUSED_", "WAITING_", "COMPLETED", "FAILED")) or status in {
            "CANCELLED", "TIMEOUT", "POLICY_BLOCKED"
        }:
            return False, "RUN_NOT_ACTIVE"
        active = int(
            await session.scalar(
                select(func.count())
                .select_from(ToolCall)
                .where(ToolCall.run_id == run.id, ToolCall.status.in_(("REQUESTED", "STARTED")))
            )
            or 0
        )
        if active:
            return False, "TOOL_IN_FLIGHT"
        return True, None

    async def protected_ids(self, session, run: SolveRun) -> dict[str, set[str]]:
        candidates = list((await session.scalars(select(FlagCandidate).where(FlagCandidate.run_id == run.id))).all())
        observations = list((await session.scalars(select(Observation).where(Observation.run_id == run.id))).all())
        artifacts = list((await session.scalars(select(Artifact).where(Artifact.run_id == run.id))).all())
        recent_logical = list(
            (
                await session.scalars(
                    select(LogicalToolCall)
                    .where(LogicalToolCall.run_id == run.id)
                    .order_by(LogicalToolCall.created_at.desc())
                    .limit(self.RECENT_TAIL_LOGICAL_CALLS)
                )
            ).all()
        )
        recent_ids = {item.id for item in recent_logical}
        recent_tool_ids = {
            item.id
            for item in (
                await session.scalars(
                    select(ToolCall)
                    .where(ToolCall.run_id == run.id)
                    .order_by(ToolCall.created_at.desc())
                    .limit(self.RECENT_TAIL_LOGICAL_CALLS)
                )
            ).all()
        }
        candidate_artifacts = {item.source_artifact_id for item in candidates if item.source_artifact_id}
        protected_artifacts = candidate_artifacts | {
            item.id
            for item in artifacts
            if item.file_path.startswith(("evidence/", "final/"))
            or item.artifact_type in {"script", "writeup", "fresh_reproduction"}
        }
        protected_observations = {
            item.id
            for item in observations
            if item.artifact_id in protected_artifacts
            or item.tool_call_id in recent_tool_ids
            or item.id in {logical.result_observation_id for logical in recent_logical if logical.result_observation_id}
        }
        protected_tools = recent_tool_ids | {
            item.tool_call_id for item in observations if item.id in protected_observations and item.tool_call_id
        }
        protected_traces = recent_ids
        recent_event_ids = {
            item.id
            for item in (
                await session.scalars(
                    select(RunEvent)
                    .where(RunEvent.run_id == run.id)
                    .order_by(RunEvent.created_at.desc())
                    .limit(self.RECENT_TAIL_EVENTS)
                )
            ).all()
        }
        return {
            "tool": protected_tools,
            "observation": protected_observations,
            "artifact": protected_artifacts,
            "event": recent_event_ids,
            "logical": recent_ids,
            "trace_logical": protected_traces,
        }

    async def validate_decision(self, session, run: SolveRun, decision: CompactionDecisionAction) -> CompactionDecisionAction:
        protected = await self.protected_ids(session, run)
        decision.keep_tool_call_ids = sorted(set(decision.keep_tool_call_ids) | protected["tool"])
        decision.keep_observation_ids = sorted(set(decision.keep_observation_ids) | protected["observation"])
        decision.keep_artifact_ids = sorted(set(decision.keep_artifact_ids) | protected["artifact"])
        # Keep recent logical calls in the snapshot's tail even though logical
        # rows themselves are never deleted (they are the idempotency ledger).
        return decision

    async def _snapshot(self, session, run: SolveRun, generation: int, decision: CompactionDecisionAction) -> dict:
        snapshot = empty_evidence_snapshot(generation)
        state = await session.scalar(select(SolverState).where(SolverState.run_id == run.id))
        if state:
            snapshot.update(
                {
                    "canonical_facts": state.confirmed_facts_json or [],
                    "confirmed_capabilities": state.capability_ledger_json or {},
                    "active_hypotheses": state.active_hypotheses_json or [],
                    "rejected_paths": state.rejected_paths_json or [],
                    "current_exploit_plan": state.attack_chain_plan_json or state.run_plan_json or {},
                    "automation_state": state.last_experiment_json or {},
                    "next_actions": (state.last_decision_card_json or {}).get("next_actions", []),
                }
            )
        snapshot.update(
            {
                "attack_chain": decision.attack_chain_summary,
                "scripts": decision.script_paths,
                "critical_artifacts": decision.keep_artifact_ids,
                "wp_critical_steps": decision.wp_critical_evidence_ids,
                "recent_errors": decision.recent_failures,
                "next_actions": decision.next_actions,
            }
        )
        return snapshot

    async def _incremental_rows(self, session, run: SolveRun) -> dict[str, list]:
        event_filter = [RunEvent.run_id == run.id]
        if int(run.last_compacted_event_id or 0):
            event_filter.append(or_(RunEvent.event_id > int(run.last_compacted_event_id), RunEvent.event_id.is_(None)))
        tool_filter = [ToolCall.run_id == run.id]
        if run.last_compacted_tool_created_at:
            tool_filter.append(ToolCall.created_at > run.last_compacted_tool_created_at)
        obs_filter = [Observation.run_id == run.id]
        if run.last_compacted_observation_created_at:
            obs_filter.append(Observation.created_at > run.last_compacted_observation_created_at)
        artifact_filter = [Artifact.run_id == run.id]
        if run.last_compacted_artifact_created_at:
            artifact_filter.append(Artifact.created_at > run.last_compacted_artifact_created_at)
        trace_filter = [LogicalToolCall.run_id == run.id]
        if run.last_compacted_trace_created_at:
            trace_filter.append(ToolExecutionTrace.created_at > run.last_compacted_trace_created_at)
        rows = {
            "run-events.jsonl.gz": list((await session.scalars(select(RunEvent).where(*event_filter).order_by(RunEvent.created_at))).all()),
            "tool-calls.jsonl.gz": list((await session.scalars(select(ToolCall).where(*tool_filter).order_by(ToolCall.created_at))).all()),
            "observations.jsonl.gz": list((await session.scalars(select(Observation).where(*obs_filter).order_by(Observation.created_at))).all()),
            "traces.jsonl.gz": list(
                (
                    await session.scalars(
                        select(ToolExecutionTrace)
                        .join(LogicalToolCall, ToolExecutionTrace.logical_tool_call_id == LogicalToolCall.id)
                        .where(*trace_filter)
                        .order_by(ToolExecutionTrace.created_at)
                    )
                ).all()
            ),
            "artifacts": list((await session.scalars(select(Artifact).where(*artifact_filter).order_by(Artifact.created_at))).all()),
        }
        return rows

    async def _delete_incremental(self, session, rows: dict[str, list], protected: dict[str, set[str]]) -> dict[str, int]:
        observations = [item for item in rows["observations.jsonl.gz"] if item.id not in protected["observation"]]
        artifacts = [item for item in rows["artifacts"] if item.id not in protected["artifact"]]
        tools = [item for item in rows["tool-calls.jsonl.gz"] if item.id not in protected["tool"]]
        traces = [item for item in rows["traces.jsonl.gz"] if item.logical_tool_call_id not in protected.get("trace_logical", set())]
        events = [item for item in rows["run-events.jsonl.gz"] if item.id not in protected["event"]]
        # Remove references in the idempotency ledger before deleting result
        # observations.  Logical rows remain as the durable deduplication key.
        observation_ids = {item.id for item in observations}
        if observation_ids:
            await session.execute(
                update(LogicalToolCall)
                .where(LogicalToolCall.result_observation_id.in_(observation_ids))
                .values(result_observation_id=None)
            )
        deleted = {}
        for label, model, selected in (
            ("observations", Observation, observations),
            ("artifacts", Artifact, artifacts),
            ("tool_calls", ToolCall, tools),
            ("traces", ToolExecutionTrace, traces),
            ("events", RunEvent, events),
        ):
            count = 0
            for offset in range(0, len(selected), self.MAX_DELETE_BATCH):
                batch = selected[offset : offset + self.MAX_DELETE_BATCH]
                ids = [item.id for item in batch]
                if ids:
                    await session.execute(delete(model).where(model.id.in_(ids)))
                    count += len(ids)
            deleted[label] = count
        return deleted

    async def apply(self, session, run: SolveRun, decision: CompactionDecisionAction | dict, *, reason: str | None = None) -> dict:
        safe, failure = await self.safe_point(session, run)
        if not safe:
            raise DomainError("COMPACTION_NOT_SAFE", "Compaction is allowed only at an idle active safe point.", {"reason": failure}, 409)
        if isinstance(decision, dict):
            decision = CompactionDecisionAction.model_validate(decision)
        decision = await self.validate_decision(session, run, decision)
        generation = int(run.compaction_generation or 0) + 1
        rows = await self._incremental_rows(session, run)
        protected = await self.protected_ids(session, run)
        checkpoint = RunCompactionCheckpoint(
            run_id=run.id,
            generation=generation,
            status="COMPACTION_APPLY",
            reason=reason or decision.compaction_reason,
            decision_json=decision.model_dump(),
            archive_manifest_json={},
            deleted_row_counts_json={},
        )
        session.add(checkpoint)
        run.compaction_status = "COMPACTION_APPLY"
        run.compaction_started_at = datetime.now(UTC)
        run.compaction_generation = generation
        await session.flush()

        root = Path(run.workspace_path).resolve()
        archive_dir = root / "archive" / "compaction" / str(generation)
        archive_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "run_id": run.id,
            "generation": generation,
            "schema_version": "0019",
            "restorable": True,
            "incremental": True,
            "created_at": datetime.now(UTC).isoformat(),
            "row_counts": {},
            "sha256": {},
            "watermarks_before": {
                "event_id": int(run.last_compacted_event_id or 0),
                "tool_created_at": str(run.last_compacted_tool_created_at) if run.last_compacted_tool_created_at else None,
                "observation_created_at": str(run.last_compacted_observation_created_at) if run.last_compacted_observation_created_at else None,
                "trace_created_at": str(run.last_compacted_trace_created_at) if run.last_compacted_trace_created_at else None,
            },
        }
        for filename, selected in ((name, rows[name]) for name in self._archive_sets):
            target = archive_dir / filename
            with gzip.open(target, "wt", encoding="utf-8") as handle:
                for row in selected:
                    handle.write(json.dumps(self._json_row(row), ensure_ascii=False, default=str) + "\n")
            manifest["row_counts"][filename] = len(selected)
            manifest["sha256"][filename] = hashlib.sha256(target.read_bytes()).hexdigest()
        (archive_dir / "artifacts-manifest.json").write_text(
            json.dumps({"row_count": len(rows["artifacts"]), "artifacts": [self._json_row(item) for item in rows["artifacts"]]}, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        snapshot_json = await self._snapshot(session, run, generation, decision)
        snapshot_bytes = json.dumps(snapshot_json, ensure_ascii=False, sort_keys=True).encode()
        snapshot = EvidenceSnapshot(
            run_id=run.id,
            generation=generation,
            snapshot_json=snapshot_json,
            sha256=hashlib.sha256(snapshot_bytes).hexdigest(),
            source_checkpoint_id=checkpoint.id,
            is_current=True,
        )
        await session.execute(update(EvidenceSnapshot).where(EvidenceSnapshot.run_id == run.id, EvidenceSnapshot.is_current.is_(True)).values(is_current=False))
        session.add(snapshot)
        await session.flush()
        manifest["snapshot_id"] = snapshot.id
        (archive_dir / "compaction-decision.json").write_text(json.dumps(decision.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
        (archive_dir / "evidence-snapshot.json").write_text(json.dumps(snapshot_json, ensure_ascii=False, indent=2), encoding="utf-8")
        deleted = await self._delete_incremental(session, rows, protected)
        manifest["deleted_row_counts"] = deleted
        checkpoint.deleted_row_counts_json = deleted

        event_rows = rows["run-events.jsonl.gz"]
        if event_rows:
            event_ids = [item.event_id for item in event_rows if item.event_id is not None]
            if event_ids:
                run.last_compacted_event_id = max(event_ids)
        for attr, key in (
            ("last_compacted_tool_created_at", "tool-calls.jsonl.gz"),
            ("last_compacted_observation_created_at", "observations.jsonl.gz"),
            ("last_compacted_trace_created_at", "traces.jsonl.gz"),
            ("last_compacted_artifact_created_at", "artifacts"),
        ):
            if rows[key]:
                setattr(run, attr, max(item.created_at for item in rows[key]))
        total_logical = int(await session.scalar(select(func.count()).select_from(LogicalToolCall).where(LogicalToolCall.run_id == run.id, LogicalToolCall.counts_toward_budget.is_(True))) or 0)
        run.last_compaction_effective_tool_count = total_logical
        run.compacted_event_count = int(run.compacted_event_count or 0) + len(event_rows)
        run.compacted_observation_count = int(run.compacted_observation_count or 0) + len(rows["observations.jsonl.gz"])
        run.compacted_artifact_count = int(run.compacted_artifact_count or 0) + len(rows["artifacts"])
        run.compacted_trace_count = int(run.compacted_trace_count or 0) + len(rows["traces.jsonl.gz"])
        run.last_compaction_snapshot_id = snapshot.id
        checkpoint.archive_path = str(archive_dir)
        checkpoint.archive_manifest_json = manifest
        checkpoint.status = "COMPLETED"
        checkpoint.finished_at = datetime.now(UTC)
        run.compaction_status = "COMPLETED"
        run.compaction_finished_at = datetime.now(UTC)
        (archive_dir / "archive-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        await session.commit()
        await event_service.append(session, run.id, "run.compaction_completed", {"generation": generation, "snapshot_id": snapshot.id, "archive_path": str(archive_dir), "deleted_row_counts": deleted})
        return {"generation": generation, "status": checkpoint.status, "archive_path": str(archive_dir), "snapshot_id": snapshot.id, "manifest": manifest}

    async def maybe_auto_compact(self, session, run: SolveRun) -> dict | None:
        triggered, metrics = await self.should_compact(session, run)
        if not triggered:
            return None
        safe, _ = await self.safe_point(session, run)
        if not safe:
            return None
        return await self.apply(session, run, CompactionDecisionAction(compaction_reason="automatic_threshold",), reason="automatic_threshold")

    async def restore_latest_snapshot(self, session, run_id: str) -> dict | None:
        snapshot = await session.scalar(select(EvidenceSnapshot).where(EvidenceSnapshot.run_id == run_id).order_by(EvidenceSnapshot.generation.desc()))
        if snapshot is None:
            return None
        raw = json.dumps(snapshot.snapshot_json, ensure_ascii=False, sort_keys=True).encode()
        if hashlib.sha256(raw).hexdigest() != snapshot.sha256:
            raise DomainError("COMPACTION_SNAPSHOT_CORRUPT", "Evidence snapshot hash verification failed.", status_code=500)
        return snapshot.snapshot_json


compaction_service = CompactionService()
