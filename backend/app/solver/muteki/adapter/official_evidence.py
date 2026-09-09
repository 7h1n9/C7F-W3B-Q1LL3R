"""Evidence authority boundary for the optional native Muteki Worker.

The upstream Worker can produce text and execution metadata, but those values
are not authoritative evidence in this application.  Deployments that want to
enable the native Worker must inject a bridge that creates or verifies the
existing ToolCall/Artifact/Observation/Evidence chain and returns durable
evidence references.  The generic protocol contains no database writes or
fallback store; the optional SQLAlchemy implementation below uses only
existing application models and the EvidenceLedger service.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select, update

if TYPE_CHECKING:
    from ..worker.official_worker import OfficialWorkerResult


class OfficialWorkerEvidenceBridge(Protocol):
    """Application-owned adapter from a native result to Evidence references.

    Implementations must use the existing Evidence authority.  The native
    adapter passes the result for ingestion, but the bridge must return only
    durable references and must not expose raw output through audit events.
    """

    def ingest(
        self,
        *,
        run_id: str,
        worker_id: str,
        intent_id: str | None,
        result: OfficialWorkerResult,
    ) -> Sequence[str] | Awaitable[Sequence[str]]:
        """Ingest one native result and return durable evidence references."""


class SqlAlchemyOfficialEvidenceBridge:
    """Persist native Worker output through the existing Evidence chain.

    Structured ``http-evidence.json`` products are projected one record at a
    time.  Each record gets its own ToolCall, protected Artifact, Observation
    and EvidenceLedger row, while all records from one Worker share an
    AgentTask and AgentTaskResult.  Unstructured products retain the legacy
    text-artifact fallback.
    """

    def __init__(self, session: object, run: object, workspace: str | Path) -> None:
        self._session = session
        self._run = run
        self._workspace = Path(workspace).resolve()

    async def ingest(
        self,
        *,
        run_id: str,
        worker_id: str,
        intent_id: str | None,
        result: OfficialWorkerResult,
    ) -> Sequence[str]:
        """Create an idempotent, evidence-backed native Worker projection."""

        if str(getattr(self._run, "id", "")) != str(run_id):
            raise ValueError("EVIDENCE_RUN_MISMATCH")
        output = _read_official_artifact(result, self._workspace)
        if not output:
            output = str(result.output or "")
        if not output:
            raise ValueError("EVIDENCE_ARTIFACT_EMPTY")

        structured_records = _structured_records(output)
        if structured_records is not None:
            return await self._ingest_structured(
                run_id=str(run_id),
                worker_id=worker_id,
                intent_id=intent_id,
                result=result,
                records=structured_records,
            )
        return await self._ingest_text(
            run_id=str(run_id),
            worker_id=worker_id,
            intent_id=intent_id,
            result=result,
            output=output,
        )

    async def _ingest_structured(
        self,
        *,
        run_id: str,
        worker_id: str,
        intent_id: str | None,
        result: OfficialWorkerResult,
        records: Sequence[Mapping[str, Any]],
    ) -> Sequence[str]:
        from app.models.multi_agent import EvidenceLedger
        from app.models.run import Artifact, Observation, ToolCall
        from app.schemas.multi_agent import EvidenceLedgerContract
        from app.services.multi_agent import EvidenceLedgerService

        safe_worker = _safe_component(worker_id, fallback="worker")
        safe_intent = _safe_component(intent_id or "", fallback="")
        artifact_intent = safe_intent or "default"
        task = await self._get_or_create_task(
            run_id=run_id,
            worker_id=safe_worker,
            intent_id=safe_intent,
            record_count=len(records),
            structured=True,
        )
        task_id = str(task.id)
        evidence_ids: list[str] = []
        created_records = 0

        for index, raw_record in enumerate(records):
            record = _normalize_record(raw_record)
            facts = _safe_record_facts(
                record,
                worker_id=safe_worker,
                intent_id=safe_intent,
                record_index=index,
            )
            record_json = json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            digest = sha256(record_json.encode("utf-8", errors="replace")).hexdigest()
            relative_path = (
                f"evidence/native-workers/{safe_worker}/{artifact_intent}/"
                f"http-{index:04d}-{digest[:16]}.json"
            )
            target = (self._workspace / relative_path).resolve()
            if self._workspace not in target.parents:
                raise ValueError("EVIDENCE_ARTIFACT_PATH_OUT_OF_SCOPE")

            existing_artifact = await self._session.scalar(
                select(Artifact).where(
                    Artifact.run_id == run_id,
                    Artifact.file_path == relative_path,
                )
            )
            if existing_artifact is not None:
                existing_evidence = await self._session.scalar(
                    select(EvidenceLedger).where(
                        EvidenceLedger.run_id == run_id,
                        EvidenceLedger.artifact_id == existing_artifact.id,
                    )
                )
                existing_observation = await self._session.scalar(
                    select(Observation).where(
                        Observation.run_id == run_id,
                        Observation.artifact_id == existing_artifact.id,
                    )
                )
                if (
                    existing_evidence is None
                    or existing_observation is None
                    or str(existing_evidence.agent_task_id or "") != task_id
                    or str(existing_evidence.tool_call_id or "")
                    != str(existing_artifact.tool_call_id or "")
                ):
                    raise ValueError("EVIDENCE_BRIDGE_INCOMPLETE_CHAIN")
                evidence_ids.append(str(existing_evidence.id))
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(record_json, encoding="utf-8")

            tool_call_id = _stable_id(
                "tool-call", run_id, safe_worker, safe_intent, str(index), digest
            )
            now = datetime.now(UTC)
            tool_call = ToolCall(
                id=tool_call_id,
                run_id=run_id,
                tool_name="muteki_native_http",
                arguments_json=facts,
                status="COMPLETED" if facts["success"] else "FAILED",
                started_at=now,
                finished_at=now,
                execution_layer="muteki_native",
                counts_toward_budget=True,
                logical_kind="TOOL",
                provider_tool_name=str(record.get("tool") or "http_request")[:120],
                effective_tool_name=str(record.get("tool") or "http_request")[:120],
                agent_task_id=task_id,
                agent_role="RECON",
            )
            self._session.add(tool_call)
            await self._session.flush()

            artifact = Artifact(
                id=_stable_id(
                    "artifact", run_id, safe_worker, safe_intent, str(index), digest
                ),
                run_id=run_id,
                tool_call_id=tool_call.id,
                artifact_type="muteki_native_http_record",
                file_path=relative_path,
                mime_type="application/json",
                size=target.stat().st_size,
                sha256=digest,
                summary="Protected native Muteki HTTP request/observation record.",
                status="ACTIVE",
                retention_class="PROTECTED",
                temporary=False,
            )
            self._session.add(artifact)
            await self._session.flush()

            observation = Observation(
                id=_stable_id(
                    "observation", run_id, safe_worker, safe_intent, str(index), digest
                ),
                run_id=run_id,
                tool_call_id=tool_call.id,
                artifact_id=artifact.id,
                observation_type="MUTEKI_NATIVE_HTTP_OBSERVATION",
                summary=_observation_summary(facts),
                facts_json=facts,
            )
            self._session.add(observation)
            await self._session.flush()

            evidence = await EvidenceLedgerService().record(
                self._session,
                EvidenceLedgerContract(
                    evidence_id=_stable_id(
                        "evidence", run_id, safe_worker, safe_intent, str(index), digest
                    ),
                    run_id=run_id,
                    evidence_type="MUTEKI_NATIVE_HTTP",
                    artifact_id=str(artifact.id),
                    tool_call_id=str(tool_call.id),
                    agent_task_id=task_id,
                    summary="Native Muteki HTTP record retained as protected evidence.",
                    sha256=digest,
                    status="VERIFIED",
                    retention_class="PROTECTED",
                    source_chain=[
                        str(artifact.id),
                        str(tool_call.id),
                        task_id,
                    ],
                ),
            )
            evidence_ids.append(str(evidence.id))
            created_records += 1

        task.status = "COMPLETED" if result.success else "FAILED"
        await self._upsert_task_result(
            task=task,
            result=result,
            evidence_ids=evidence_ids,
            structured=True,
        )
        await _project_native_run_counters(
            self._session,
            self._run,
            result,
            logical_calls=created_records,
        )
        await self._session.commit()
        return tuple(evidence_ids)

    async def _ingest_text(
        self,
        *,
        run_id: str,
        worker_id: str,
        intent_id: str | None,
        result: OfficialWorkerResult,
        output: str,
    ) -> Sequence[str]:
        """Retain the legacy whole-product artifact for unstructured output."""

        from app.models.multi_agent import AgentTask, AgentTaskResult, EvidenceLedger
        from app.models.run import Artifact, Observation, ToolCall
        from app.schemas.multi_agent import EvidenceLedgerContract
        from app.services.multi_agent import EvidenceLedgerService

        safe_worker = _safe_component(worker_id, fallback="worker")
        safe_intent = _safe_component(intent_id or "", fallback="")
        digest = sha256(output.encode("utf-8", errors="replace")).hexdigest()
        relative_path = f"evidence/native-workers/{safe_worker}-{digest[:16]}.txt"
        target = (self._workspace / relative_path).resolve()
        if self._workspace not in target.parents:
            raise ValueError("EVIDENCE_ARTIFACT_PATH_OUT_OF_SCOPE")

        existing_artifact = await self._session.scalar(
            select(Artifact).where(
                Artifact.run_id == run_id,
                Artifact.file_path == relative_path,
            )
        )
        if existing_artifact is not None:
            existing_ledger = await self._session.scalar(
                select(EvidenceLedger).where(
                    EvidenceLedger.run_id == run_id,
                    EvidenceLedger.artifact_id == existing_artifact.id,
                )
            )
            if existing_ledger is not None:
                return (str(existing_ledger.id),)
            raise ValueError("EVIDENCE_BRIDGE_INCOMPLETE_CHAIN")

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(output, encoding="utf-8")

        task = AgentTask(
            id=_stable_id("agent-task-text", run_id, safe_worker, safe_intent),
            run_id=run_id,
            agent_role="RECON",
            task_kind="RECON",
            objective="Persist one bounded native Muteki Worker result.",
            allowed_tools_json=[],
            budget_json={
                "max_logical_calls": 1,
                "max_internal_requests": 0,
                "max_runtime_seconds": 900,
            },
            success_condition="Native Worker artifact is linked to EvidenceLedger.",
            stop_conditions_json=["missing evidence"],
            status="RUNNING",
            timeout_seconds=900,
            runtime_path="muteki_native",
            context_json={
                "runtime_path": "muteki_native",
                "worker_id": safe_worker,
                "intent_id": safe_intent[:120],
                "projection": "legacy_text",
            },
        )
        self._session.add(task)
        await self._session.flush()

        now = datetime.now(UTC)
        tool_call = ToolCall(
            id=_stable_id("tool-call-text", run_id, safe_worker, safe_intent, digest),
            run_id=run_id,
            tool_name="muteki_native_worker",
            arguments_json={
                "worker_id": safe_worker,
                "intent_id": safe_intent[:120],
                "engine": str(result.engine or "")[:80],
            },
            status="COMPLETED" if result.success else "FAILED",
            started_at=now,
            finished_at=now,
            execution_layer="muteki_native",
            counts_toward_budget=True,
            logical_kind="WORKER",
            provider_tool_name="muteki_native_worker",
            effective_tool_name="muteki_native_worker",
            agent_task_id=str(task.id),
            agent_role="RECON",
        )
        self._session.add(tool_call)
        await self._session.flush()

        artifact = Artifact(
            id=_stable_id("artifact-text", run_id, safe_worker, safe_intent, digest),
            run_id=run_id,
            tool_call_id=tool_call.id,
            artifact_type="muteki_native_worker_output",
            file_path=relative_path,
            mime_type="text/plain",
            size=target.stat().st_size,
            sha256=digest,
            summary="Bounded output from one native Muteki Worker.",
            status="ACTIVE",
            retention_class="PROTECTED",
            temporary=False,
        )
        self._session.add(artifact)
        await self._session.flush()

        observation = Observation(
            id=_stable_id("observation-text", run_id, safe_worker, safe_intent, digest),
            run_id=run_id,
            tool_call_id=tool_call.id,
            artifact_id=artifact.id,
            observation_type="MUTEKI_NATIVE_WORKER_OBSERVATION",
            summary="Native Muteki Worker product retained as a protected artifact.",
            facts_json={
                "worker_id": safe_worker,
                "intent_id": safe_intent[:120],
                "success": bool(result.success),
            },
        )
        self._session.add(observation)
        await self._session.flush()

        evidence = await EvidenceLedgerService().record(
            self._session,
            EvidenceLedgerContract(
                evidence_id=_stable_id(
                    "evidence-text", run_id, safe_worker, safe_intent, digest
                ),
                run_id=run_id,
                evidence_type="MUTEKI_NATIVE_WORKER",
                artifact_id=str(artifact.id),
                tool_call_id=str(tool_call.id),
                agent_task_id=str(task.id),
                summary="Native Muteki Worker output retained as a protected artifact.",
                sha256=digest,
                status="VERIFIED",
                retention_class="PROTECTED",
                source_chain=[
                    str(artifact.id),
                    str(tool_call.id),
                    str(task.id),
                ],
            ),
        )
        task.status = "COMPLETED" if result.success else "FAILED"
        self._session.add(
            AgentTaskResult(
                task_id=task.id,
                status=task.status,
                evidence_ids_json=[str(evidence.id)],
                handoff_summary=(
                    "Native Worker artifact was linked to the existing Evidence authority."
                ),
            )
        )
        await _project_native_run_counters(
            self._session,
            self._run,
            result,
            logical_calls=1,
        )
        await self._session.commit()
        return (str(evidence.id),)

    async def _get_or_create_task(
        self,
        *,
        run_id: str,
        worker_id: str,
        intent_id: str,
        record_count: int,
        structured: bool,
    ):
        from app.models.multi_agent import AgentTask

        task_id = _stable_id(
            "agent-task-structured" if structured else "agent-task",
            run_id,
            worker_id,
            intent_id,
        )
        task = await self._session.get(AgentTask, task_id)
        if task is not None:
            if str(task.run_id) != str(run_id):
                raise ValueError("EVIDENCE_CHAIN_INVALID")
            return task
        task = AgentTask(
            id=task_id,
            run_id=run_id,
            agent_role="RECON",
            task_kind="RECON",
            objective="Persist bounded native Muteki Worker HTTP observations.",
            allowed_tools_json=["http_request", "http_session_request"],
            budget_json={
                "max_logical_calls": max(1, int(record_count)),
                "max_internal_requests": max(1, int(record_count)),
                "max_runtime_seconds": 900,
            },
            success_condition="Native HTTP records are linked to EvidenceLedger.",
            stop_conditions_json=["missing structured evidence"],
            status="RUNNING",
            timeout_seconds=900,
            runtime_path="muteki_native",
            context_json={
                "runtime_path": "muteki_native",
                "worker_id": worker_id,
                "intent_id": intent_id[:120],
                "projection": "structured_http",
            },
        )
        self._session.add(task)
        await self._session.flush()
        return task

    async def _upsert_task_result(
        self,
        *,
        task: object,
        result: OfficialWorkerResult,
        evidence_ids: Sequence[str],
        structured: bool,
    ) -> None:
        from app.models.multi_agent import AgentTaskResult

        task_id = str(getattr(task, "id"))
        task_result = await self._session.scalar(
            select(AgentTaskResult).where(AgentTaskResult.task_id == task_id)
        )
        status = "COMPLETED" if result.success else "FAILED"
        summary = (
            "Native HTTP records were linked to the existing Evidence authority."
            if structured
            else "Native Worker artifact was linked to the existing Evidence authority."
        )
        if task_result is None:
            self._session.add(
                AgentTaskResult(
                    task_id=task_id,
                    status=status,
                    evidence_ids_json=list(evidence_ids),
                    handoff_summary=summary,
                )
            )
            return
        task_result.status = status
        task_result.evidence_ids_json = list(evidence_ids)
        task_result.handoff_summary = summary
        self._session.add(task_result)

    async def rollback(self) -> None:
        """Clear a failed SQLAlchemy transaction after bridge failure."""

        await self._session.rollback()


def sanitize_evidence_refs(value: object) -> tuple[str, ...]:
    """Return bounded, non-empty evidence reference strings.

    Evidence references are identifiers, not result bodies.  Bounding and
    type-checking them here prevents a misbehaving bridge from placing an
    arbitrary object or an unbounded CLI payload into the graph contract.
    """

    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return ()
    refs: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        ref = item.strip()
        if not ref or len(ref) > 512 or ref in refs:
            continue
        refs.append(ref)
        if len(refs) >= 32:
            break
    return tuple(refs)


def _safe_component(value: str, *, fallback: str) -> str:
    component = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "")).strip(".-")
    return (component or fallback)[:120]


def _stable_id(kind: str, *parts: str) -> str:
    material = "|".join(str(part or "") for part in parts)
    return str(uuid5(NAMESPACE_URL, f"muteki-native-evidence:{kind}:{material}"))


def _read_official_artifact(result: OfficialWorkerResult, workspace: Path) -> str:
    """Read the official artifact only inside the Evidence authority.

    Native Worker output is intentionally not copied through Graph or audit.
    This helper is the one narrow handoff where the existing Evidence chain may
    retain the official Muteki artifact as its protected source document.
    """

    value = str(getattr(result, "evidence_artifact_path", "") or "").strip()
    if not value:
        return ""
    try:
        path = Path(value).resolve()
        root = workspace.resolve()
        if root not in path.parents or not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError, ValueError):
        return ""


def _structured_records(output: str) -> list[Mapping[str, Any]] | None:
    """Return a valid non-empty structured HTTP record list, or ``None``."""

    try:
        payload = json.loads(output)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    records = payload.get("records")
    if (
        not isinstance(records, Sequence)
        or isinstance(records, (str, bytes))
        or not records
    ):
        return None
    normalized: list[Mapping[str, Any]] = []
    for item in records:
        if not isinstance(item, Mapping):
            return None
        normalized.append(item)
    return normalized


def _normalize_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the protected artifact record JSON-safe without inventing fields."""

    status_code = _optional_int(record.get("status_code"))
    return {
        "tool": _bounded_text(record.get("tool"), 80) or "http_request",
        "method": (_bounded_text(record.get("method"), 16) or "GET").upper(),
        "url": _bounded_text(record.get("url"), 2048),
        "status_code": status_code,
        "final_url": _bounded_text(record.get("final_url"), 2048),
        "headers": _safe_headers(record.get("headers")),
        "body": str(record.get("body") or ""),
    }


def _safe_record_facts(
    record: Mapping[str, Any],
    *,
    worker_id: str,
    intent_id: str,
    record_index: int,
) -> dict[str, Any]:
    """Return the only fields allowed in outer ToolCall/Observation rows."""

    status_code = record.get("status_code")
    return {
        "method": str(record.get("method") or "GET")[:16].upper(),
        "url": str(record.get("url") or "")[:2048],
        "status_code": status_code,
        "final_url": str(record.get("final_url") or "")[:2048],
        "success": status_code is not None,
        "worker_id": worker_id,
        "intent_id": intent_id[:120],
        "record_index": int(record_index),
    }


def _observation_summary(facts: Mapping[str, Any]) -> str:
    method = str(facts.get("method") or "GET")[:16]
    status_code = facts.get("status_code")
    if status_code is None:
        return f"Native Muteki {method} request did not receive an HTTP response."
    return f"Native Muteki {method} request returned HTTP {status_code}."


def _bounded_text(value: Any, limit: int) -> str:
    text = str(value or "")
    return text[:limit]


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_headers(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key)[:120]: str(item)[:4000]
        for key, item in value.items()
        if str(key).strip()
    }


async def _project_native_run_counters(
    session: object,
    run: object,
    result: OfficialWorkerResult,
    *,
    logical_calls: int = 1,
) -> None:
    """Project one native Worker product into existing Run counters.

    ``logical_calls`` counts the new outer ToolCall rows created by this
    ingest.  ``num_turns`` remains the Worker/Agent-step view and is therefore
    added once per ingest, not once per internal HTTP record.
    """

    calls = max(0, int(logical_calls))
    if not calls:
        return
    metadata = result.metadata if isinstance(result.metadata, Mapping) else {}
    try:
        turns = max(1, int(metadata.get("num_turns") or 1))
    except (TypeError, ValueError, OverflowError):
        turns = 1

    from app.models.run import SolveRun

    if isinstance(run, SolveRun):
        await session.execute(
            update(SolveRun)
            .where(SolveRun.id == str(run.id))
            .values(
                tool_call_count=SolveRun.tool_call_count + calls,
                run_total_logical_tool_calls=(
                    SolveRun.run_total_logical_tool_calls + calls
                ),
                attempt_logical_tool_calls=(
                    SolveRun.attempt_logical_tool_calls + calls
                ),
                run_total_agent_steps=SolveRun.run_total_agent_steps + turns,
                agent_step_count=SolveRun.agent_step_count + turns,
                attempt_agent_steps=SolveRun.attempt_agent_steps + turns,
                checkpoint_segment_steps=SolveRun.checkpoint_segment_steps + turns,
            )
        )
        # The native bridge shares the Supervisor session.  Refresh the
        # identity-mapped Run immediately so a later lifecycle commit cannot
        # flush its pre-update zero values back over the SQL expression.
        await session.refresh(run)
        return

    _increment(run, "tool_call_count", calls)
    _increment(run, "run_total_logical_tool_calls", calls)
    _increment(run, "attempt_logical_tool_calls", calls)
    _increment(run, "run_total_agent_steps", turns)
    _increment(run, "agent_step_count", turns)
    _increment(run, "attempt_agent_steps", turns)
    _increment(run, "checkpoint_segment_steps", turns)


def _increment(target: object, field: str, amount: int) -> None:
    try:
        current = int(getattr(target, field, 0) or 0)
    except (TypeError, ValueError, OverflowError):
        current = 0
    setattr(target, field, current + max(0, int(amount)))


__all__ = [
    "OfficialWorkerEvidenceBridge",
    "SqlAlchemyOfficialEvidenceBridge",
    "sanitize_evidence_refs",
]
