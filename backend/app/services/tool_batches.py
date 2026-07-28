"""Deduplication and aggregation for bounded batch tool executions."""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.run import Artifact, SolveRun, ToolBatchSummary, ToolRequestFingerprint
from app.services.temporary_data import temporary_workspace


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode()


class ToolScheduler:
    async def fingerprint(
        self,
        session: AsyncSession,
        run: SolveRun,
        tool_name: str,
        arguments: dict,
        *,
        target: str = "",
        stage: str | None = None,
        evidence_version: int | None = None,
        logical_tool_call_id: str | None = None,
    ) -> dict:
        normalized = {"tool": tool_name, "arguments": arguments, "target": target, "stage": stage or run.current_phase, "evidence_version": int(evidence_version if evidence_version is not None else run.context_revision)}
        digest = hashlib.sha256(_canonical(normalized)).hexdigest()
        existing = await session.scalar(select(ToolRequestFingerprint).where(ToolRequestFingerprint.run_id == run.id, ToolRequestFingerprint.fingerprint == digest))
        now = datetime.now(UTC)
        if existing:
            existing.last_seen_at = now
            return {"status": "DUPLICATE_TOOL_REQUEST", "fingerprint": digest, "logical_tool_call_id": existing.logical_tool_call_id, "reused": True}
        logical_id = logical_tool_call_id or f"LC-{uuid4()}"
        session.add(ToolRequestFingerprint(run_id=run.id, fingerprint=digest, tool_name=tool_name, normalized_arguments_json=normalized, stage=str(stage or run.current_phase), evidence_version=int(normalized["evidence_version"]), logical_tool_call_id=logical_id, last_seen_at=now))
        await session.flush()
        return {"status": "SCHEDULED", "fingerprint": digest, "logical_tool_call_id": logical_id, "reused": False}


class ToolSubrequestAggregator:
    async def aggregate(
        self,
        session: AsyncSession,
        run: SolveRun,
        *,
        task_id: str | None,
        logical_tool_call_id: str,
        tool_call_id: str | None,
        tool_name: str,
        subrequests: list[dict],
        result: dict | None = None,
    ) -> dict:
        existing = await session.scalar(select(ToolBatchSummary).where(ToolBatchSummary.run_id == run.id, ToolBatchSummary.logical_tool_call_id == logical_tool_call_id))
        if existing:
            return {"status": "DUPLICATE_BATCH", "summary_id": existing.id, "artifact_id": existing.result_artifact_id}
        task_root = temporary_workspace.ensure_layout(Path(run.workspace_path)) / "tool-subrequests" / (tool_call_id or logical_tool_call_id)
        task_root.mkdir(parents=True, exist_ok=True)
        raw_path = task_root / "requests.jsonl.gz"
        success = failure = retries = bytes_received = duration_ms = 0
        with gzip.open(raw_path, "wt", encoding="utf-8") as handle:
            for item in subrequests:
                payload = dict(item)
                status = str(payload.get("status") or "COMPLETED").upper()
                success += int(status in {"COMPLETED", "SUCCESS", "OK"})
                failure += int(status not in {"COMPLETED", "SUCCESS", "OK"})
                retries += int(payload.get("retry_count") or payload.get("retries") or 0)
                bytes_received += int(payload.get("bytes_received") or len(str(payload.get("body") or "").encode()))
                duration_ms += int(payload.get("duration_ms") or payload.get("runtime_ms") or 0)
                handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str) + "\n")
        aggregate_root = Path(run.workspace_path).resolve() / "outputs" / "aggregated"
        aggregate_root.mkdir(parents=True, exist_ok=True)
        aggregate_relative = f"outputs/aggregated/{tool_name}-{logical_tool_call_id}.json"
        aggregate_path = Path(run.workspace_path).resolve() / aggregate_relative
        aggregate_payload = {"subrequest_count": len(subrequests), "success_count": success, "failure_count": failure, "retry_count": retries, "bytes_received": bytes_received, "duration_ms": duration_ms, "result": result or {}, "raw_subrequest_path": str(raw_path.relative_to(Path(run.workspace_path).resolve())).replace("\\", "/")}
        aggregate_path.write_text(json.dumps(aggregate_payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        raw = aggregate_path.read_bytes()
        artifact = Artifact(run_id=run.id, tool_call_id=tool_call_id, artifact_type="tool_batch_aggregate", file_path=aggregate_relative, mime_type="application/json", size=len(raw), sha256=hashlib.sha256(raw).hexdigest(), summary=f"Aggregated {len(subrequests)} subrequests for {tool_name}", retention_class="PROTECTED", temporary=False)
        session.add(artifact)
        await session.flush()
        summary = ToolBatchSummary(run_id=run.id, agent_task_id=task_id, logical_tool_call_id=logical_tool_call_id, tool_call_id=tool_call_id, tool_name=tool_name, subrequest_count=len(subrequests), success_count=success, failure_count=failure, retry_count=retries, bytes_received=bytes_received, duration_ms=duration_ms, result_artifact_id=artifact.id, result_artifact_path=aggregate_relative, status="COMPLETED" if failure == 0 else "PARTIAL")
        session.add(summary)
        await session.flush()
        return {"status": "AGGREGATED", "summary_id": summary.id, "artifact_id": artifact.id, "subrequest_count": len(subrequests), "success_count": success, "failure_count": failure, "retry_count": retries, "bytes_received": bytes_received, "duration_ms": duration_ms, "result_artifact_path": aggregate_relative, "temporary_raw_path": str(raw_path.relative_to(Path(run.workspace_path).resolve())).replace("\\", "/")}


tool_scheduler = ToolScheduler()
tool_subrequest_aggregator = ToolSubrequestAggregator()
