"""Model-reviewed, archive-before-delete run compaction."""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select, update
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
    DEFAULT_THRESHOLD = 20
    MAX_EVENTS = 800
    MAX_OBSERVATIONS = 150
    MAX_ARTIFACTS = 150
    MAX_TRACES = 1000
    MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
    MAX_DELETE_BATCH = 200

    _archive_sets = {
        "run-events.jsonl.gz": RunEvent,
        "tool-calls.jsonl.gz": ToolCall,
        "observations.jsonl.gz": Observation,
        "traces.jsonl.gz": ToolExecutionTrace,
    }

    @staticmethod
    def _json_row(row) -> dict:
        return {attr.key: getattr(row, attr.key) for attr in sa_inspect(row).mapper.column_attrs}

    async def metrics(self, session, run: SolveRun) -> dict:
        logical = int(await session.scalar(select(func.count(func.distinct(LogicalToolCall.id))).where(LogicalToolCall.run_id == run.id)) or 0)
        all_events = list((await session.scalars(select(RunEvent).where(RunEvent.run_id == run.id))).all())
        events = [item for item in all_events if not run.compaction_finished_at or item.created_at > run.compaction_finished_at]
        observations_total = int(await session.scalar(select(func.count()).select_from(Observation).where(Observation.run_id == run.id)) or 0)
        artifacts_total = int(await session.scalar(select(func.count()).select_from(Artifact).where(Artifact.run_id == run.id)) or 0)
        traces_total = int(await session.scalar(select(func.count()).select_from(ToolExecutionTrace).join(LogicalToolCall, ToolExecutionTrace.logical_tool_call_id == LogicalToolCall.id).where(LogicalToolCall.run_id == run.id)) or 0)
        observations = max(0, observations_total - int(run.compacted_observation_count or 0))
        artifacts = max(0, artifacts_total - int(run.compacted_artifact_count or 0))
        traces = max(0, traces_total - int(run.compacted_trace_count or 0))
        payload_bytes = sum(len(json.dumps(item.payload_json or {}, ensure_ascii=False, default=str).encode()) for item in events)
        return {
            "effective_logical_calls_since_last_compaction": max(0, logical - int(run.last_compaction_effective_tool_count or 0)),
            "events_since_last_compaction": len(events),
            "observations_since_last_compaction": observations,
            "artifacts_since_last_compaction": artifacts,
            "traces_since_last_compaction": traces,
            "payload_bytes_since_last_compaction": payload_bytes,
        }

    async def should_compact(self, session, run: SolveRun) -> tuple[bool, dict]:
        metrics = await self.metrics(session, run)
        triggered = (
            metrics["effective_logical_calls_since_last_compaction"] >= self.DEFAULT_THRESHOLD
            or metrics["events_since_last_compaction"] >= self.MAX_EVENTS
            or metrics["observations_since_last_compaction"] >= self.MAX_OBSERVATIONS
            or metrics["artifacts_since_last_compaction"] >= self.MAX_ARTIFACTS
            or metrics["traces_since_last_compaction"] >= self.MAX_TRACES
            or metrics["payload_bytes_since_last_compaction"] >= self.MAX_PAYLOAD_BYTES
        )
        return triggered, metrics

    async def safe_point(self, session, run: SolveRun) -> tuple[bool, str | None]:
        status = str(run.status or "")
        if status.startswith("PAUSED_") or status.startswith("WAITING_") or status.startswith("COMPLETED") or status.startswith("FAILED") or status in {"CANCELLED", "TIMEOUT", "POLICY_BLOCKED"}:
            return False, "RUN_NOT_ACTIVE"
        active = int(await session.scalar(select(func.count()).select_from(ToolCall).where(ToolCall.run_id == run.id, ToolCall.status.in_(("REQUESTED", "STARTED")))) or 0)
        if active:
            return False, "TOOL_IN_FLIGHT"
        # A queued input is a user-visible boundary and must not be folded into
        # a snapshot while it is being consumed by the coordinator.
        return True, None

    async def protected_ids(self, session, run: SolveRun) -> dict[str, set[str]]:
        candidates = list((await session.scalars(select(FlagCandidate).where(FlagCandidate.run_id == run.id))).all())
        observations = list((await session.scalars(select(Observation).where(Observation.run_id == run.id))).all())
        artifacts = list((await session.scalars(select(Artifact).where(Artifact.run_id == run.id))).all())
        logical = list((await session.scalars(select(LogicalToolCall).where(LogicalToolCall.run_id == run.id).order_by(LogicalToolCall.created_at.desc()).limit(10))).all())
        tools = list((await session.scalars(select(ToolCall).where(ToolCall.run_id == run.id, ToolCall.status == "FAILED").order_by(ToolCall.created_at.desc()).limit(1))).all())
        return {
            "tool": {item.id for item in tools} | {item.id for item in observations if item.tool_call_id},
            "observation": {item.id for item in observations if item.artifact_id or item.tool_call_id},
            "artifact": {item.id for item in artifacts if any(item.id == candidate.source_artifact_id for candidate in candidates)} | {item.id for item in artifacts if item.file_path.startswith(("evidence/", "final/"))},
            "event": set(),
            "logical": {item.id for item in logical},
        }

    async def validate_decision(self, session, run: SolveRun, decision: CompactionDecisionAction) -> CompactionDecisionAction:
        protected = await self.protected_ids(session, run)
        # Validator auto-fills omissions instead of allowing the model to
        # accidentally discard flag/WP evidence or the recent tail.
        decision.keep_tool_call_ids = sorted(set(decision.keep_tool_call_ids) | protected["tool"])
        decision.keep_observation_ids = sorted(set(decision.keep_observation_ids) | protected["observation"])
        decision.keep_artifact_ids = sorted(set(decision.keep_artifact_ids) | protected["artifact"])
        decision.keep_tool_call_ids = sorted(set(decision.keep_tool_call_ids) | protected["logical"])
        return decision

    async def _snapshot(self, session, run: SolveRun, generation: int, decision: CompactionDecisionAction) -> dict:
        snapshot = empty_evidence_snapshot(generation)
        state = await session.scalar(select(SolverState).where(SolverState.run_id == run.id))
        if state:
            snapshot.update({
                "canonical_facts": state.confirmed_facts_json or [],
                "confirmed_capabilities": state.capability_ledger_json or {},
                "active_hypotheses": state.active_hypotheses_json or [],
                "rejected_paths": state.rejected_paths_json or [],
                "current_exploit_plan": state.attack_chain_plan_json or state.run_plan_json or {},
                "automation_state": state.last_experiment_json or {},
                "next_actions": (state.last_decision_card_json or {}).get("next_actions", []),
            })
        snapshot["attack_chain"] = decision.attack_chain_summary
        snapshot["scripts"] = decision.script_paths
        snapshot["critical_artifacts"] = decision.keep_artifact_ids
        snapshot["wp_critical_steps"] = decision.wp_critical_evidence_ids
        snapshot["recent_errors"] = decision.recent_failures
        snapshot["next_actions"] = decision.next_actions
        return snapshot

    async def apply(self, session, run: SolveRun, decision: CompactionDecisionAction | dict, *, reason: str | None = None) -> dict:
        safe, failure = await self.safe_point(session, run)
        if not safe:
            raise DomainError("COMPACTION_NOT_SAFE", "Compaction is allowed only at an idle active safe point.", {"reason": failure}, 409)
        if isinstance(decision, dict):
            decision = CompactionDecisionAction.model_validate(decision)
        decision = await self.validate_decision(session, run, decision)
        generation = int(run.compaction_generation or 0) + 1
        checkpoint = RunCompactionCheckpoint(run_id=run.id, generation=generation, status="COMPACTION_APPLY", reason=reason or decision.compaction_reason, decision_json=decision.model_dump(), archive_manifest_json={}, deleted_row_counts_json={})
        session.add(checkpoint)
        run.compaction_status = "COMPACTION_APPLY"
        run.compaction_started_at = datetime.now(UTC)
        run.compaction_generation = generation
        await session.flush()

        root = Path(run.workspace_path).resolve()
        archive_dir = root / "archive" / "compaction" / str(generation)
        archive_dir.mkdir(parents=True, exist_ok=True)
        manifest = {"run_id": run.id, "generation": generation, "schema_version": "0018", "restorable": True, "created_at": datetime.now(UTC).isoformat(), "row_counts": {}, "sha256": {}}
        rows_by_name = {}
        for filename, model in self._archive_sets.items():
            rows = list((await session.scalars(select(model).where(model.run_id == run.id))).all()) if model is not ToolExecutionTrace else list((await session.scalars(select(model).join(LogicalToolCall, model.logical_tool_call_id == LogicalToolCall.id).where(LogicalToolCall.run_id == run.id))).all())
            rows_by_name[filename] = rows
            target = archive_dir / filename
            with gzip.open(target, "wt", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(self._json_row(row), ensure_ascii=False, default=str) + "\n")
            manifest["row_counts"][filename] = len(rows)
            manifest["sha256"][filename] = hashlib.sha256(target.read_bytes()).hexdigest()

        snapshot_json = await self._snapshot(session, run, generation, decision)
        snapshot_bytes = json.dumps(snapshot_json, ensure_ascii=False, sort_keys=True).encode()
        snapshot = EvidenceSnapshot(run_id=run.id, generation=generation, snapshot_json=snapshot_json, sha256=hashlib.sha256(snapshot_bytes).hexdigest(), source_checkpoint_id=checkpoint.id, is_current=True)
        await session.execute(
            update(EvidenceSnapshot)
            .where(EvidenceSnapshot.run_id == run.id, EvidenceSnapshot.is_current.is_(True))
            .values(is_current=False)
        )
        session.add(snapshot)
        await session.flush()
        manifest["snapshot_id"] = snapshot.id
        artifacts = list((await session.scalars(select(Artifact).where(Artifact.run_id == run.id))).all())
        (archive_dir / "artifacts-manifest.json").write_text(
            json.dumps({"row_count": len(artifacts), "artifacts": [self._json_row(item) for item in artifacts]}, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        (archive_dir / "compaction-decision.json").write_text(json.dumps(decision.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
        (archive_dir / "evidence-snapshot.json").write_text(json.dumps(snapshot_json, ensure_ascii=False, indent=2), encoding="utf-8")
        (archive_dir / "archive-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        checkpoint.archive_path = str(archive_dir)
        checkpoint.archive_manifest_json = manifest
        checkpoint.status = "COMPLETED"
        checkpoint.finished_at = datetime.now(UTC)
        run.compaction_status = "COMPLETED"
        run.compaction_finished_at = datetime.now(UTC)
        run.last_compaction_effective_tool_count = int((await self.metrics(session, run))["effective_logical_calls_since_last_compaction"] + (run.last_compaction_effective_tool_count or 0))
        run.compacted_event_count = manifest["row_counts"].get("run-events.jsonl.gz", 0)
        run.compacted_observation_count = manifest["row_counts"].get("observations.jsonl.gz", 0)
        run.compacted_artifact_count = len(artifacts)
        run.compacted_trace_count = manifest["row_counts"].get("traces.jsonl.gz", 0)
        run.last_compaction_snapshot_id = snapshot.id
        await session.commit()
        await event_service.append(session, run.id, "run.compaction_completed", {"generation": generation, "snapshot_id": snapshot.id, "archive_path": str(archive_dir)})
        return {"generation": generation, "status": checkpoint.status, "archive_path": str(archive_dir), "snapshot_id": snapshot.id, "manifest": manifest}

    async def restore_latest_snapshot(self, session, run_id: str) -> dict | None:
        snapshot = await session.scalar(select(EvidenceSnapshot).where(EvidenceSnapshot.run_id == run_id).order_by(EvidenceSnapshot.generation.desc()))
        if snapshot is None:
            return None
        raw = json.dumps(snapshot.snapshot_json, ensure_ascii=False, sort_keys=True).encode()
        if hashlib.sha256(raw).hexdigest() != snapshot.sha256:
            raise DomainError("COMPACTION_SNAPSHOT_CORRUPT", "Evidence snapshot hash verification failed.", status_code=500)
        return snapshot.snapshot_json


compaction_service = CompactionService()
