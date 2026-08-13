"""Evidence authority boundary for the optional native Muteki Worker.

The upstream Worker can produce text and execution metadata, but those values
are not authoritative evidence in this application.  Deployments that want to
enable the native Worker must inject a bridge that creates or verifies the
existing ToolCall/Artifact/Evidence chain and returns durable evidence
references.  The generic protocol contains no database writes or fallback
store; the optional SQLAlchemy implementation below uses only existing
application models and the EvidenceLedger service.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from sqlalchemy import update

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
        """Ingest one successful native result and return evidence references."""


class SqlAlchemyOfficialEvidenceBridge:
    """Persist native Worker output through the existing Evidence chain.

    This is an opt-in production adapter.  It creates ordinary existing
    ``AgentTask``, ``ToolCall``, ``Artifact``, ``AgentTaskResult`` and
    ``EvidenceLedger`` rows; it does not add a table or treat CLI text as a
    verified finding.  The artifact is retained under the run workspace and
    only its EvidenceLedger id is returned to the native Worker boundary.
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
        """Create one idempotent, evidence-backed native Worker artifact."""

        from sqlalchemy import select

        from app.models.multi_agent import AgentTask, AgentTaskResult, EvidenceLedger
        from app.models.run import Artifact, ToolCall
        from app.schemas.multi_agent import EvidenceLedgerContract
        from app.services.multi_agent import EvidenceLedgerService

        if str(getattr(self._run, "id", "")) != str(run_id):
            raise ValueError("EVIDENCE_RUN_MISMATCH")
        output = _read_official_artifact(result, self._workspace)
        if not output:
            output = str(result.output or "")
        if not output:
            raise ValueError("EVIDENCE_ARTIFACT_EMPTY")

        digest = sha256(output.encode("utf-8", errors="replace")).hexdigest()
        safe_worker = _safe_component(worker_id, fallback="worker")
        relative_path = f"evidence/native-workers/{safe_worker}-{digest[:16]}.txt"
        root = self._workspace
        target = (root / relative_path).resolve()
        if root not in target.parents:
            raise ValueError("EVIDENCE_ARTIFACT_PATH_OUT_OF_SCOPE")

        existing_artifact = await self._session.scalar(
            select(Artifact).where(
                Artifact.run_id == str(run_id),
                Artifact.file_path == relative_path,
            )
        )
        if existing_artifact is not None:
            existing_ledger = await self._session.scalar(
                select(EvidenceLedger).where(
                    EvidenceLedger.run_id == str(run_id),
                    EvidenceLedger.artifact_id == existing_artifact.id,
                )
            )
            if existing_ledger is not None:
                return [str(existing_ledger.id)]
            raise ValueError("EVIDENCE_BRIDGE_INCOMPLETE_CHAIN")

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(output, encoding="utf-8")
        task = AgentTask(
            run_id=str(run_id),
            agent_role="RECON",
            task_kind="RECON",
            objective="Persist one bounded native Muteki Worker result.",
            allowed_tools_json=[],
            budget_json={"max_logical_calls": 1, "max_internal_requests": 0, "max_runtime_seconds": 900},
            success_condition="Native Worker artifact is linked to EvidenceLedger.",
            stop_conditions_json=["missing evidence"],
            status="RUNNING",
            timeout_seconds=900,
            context_json={
                "runtime_path": "muteki_native",
                "worker_id": _safe_component(worker_id, fallback="worker"),
                "intent_id": _safe_component(intent_id or "", fallback="")[:120],
            },
        )
        self._session.add(task)
        await self._session.flush()

        now = datetime.now(UTC)
        tool_call = ToolCall(
            run_id=str(run_id),
            tool_name="muteki_native_worker",
            arguments_json={
                "worker_id": _safe_component(worker_id, fallback="worker"),
                "intent_id": _safe_component(intent_id or "", fallback="")[:120],
                "engine": str(result.engine or "")[:80],
            },
            status="COMPLETED",
            started_at=now,
            finished_at=now,
            execution_layer="muteki_native",
            counts_toward_budget=True,
            logical_kind="WORKER",
            provider_tool_name="muteki_native_worker",
            effective_tool_name="muteki_native_worker",
            agent_task_id=task.id,
            agent_role="RECON",
        )
        self._session.add(tool_call)
        await self._session.flush()

        artifact = Artifact(
            run_id=str(run_id),
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

        evidence = await EvidenceLedgerService().record(
            self._session,
            EvidenceLedgerContract(
                evidence_id=str(uuid4()),
                run_id=str(run_id),
                evidence_type="MUTEKI_NATIVE_WORKER",
                artifact_id=str(artifact.id),
                tool_call_id=str(tool_call.id),
                agent_task_id=str(task.id),
                summary="Native Muteki Worker output retained as a protected artifact.",
                sha256=digest,
                status="VERIFIED",
                retention_class="PROTECTED",
                source_chain=[str(artifact.id), str(tool_call.id), str(task.id)],
            ),
        )
        task.status = "COMPLETED"
        self._session.add(
            AgentTaskResult(
                task_id=task.id,
                status="COMPLETED",
                evidence_ids_json=[str(evidence.id)],
                handoff_summary="Native Worker artifact was linked to the existing Evidence authority.",
            )
        )
        await _project_native_run_counters(self._session, self._run, result)
        await self._session.commit()
        return [str(evidence.id)]

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


async def _project_native_run_counters(
    session: object,
    run: object,
    result: OfficialWorkerResult,
) -> None:
    """Project one native Worker product into existing Run counters.

    The native Worker can execute several shell/tool calls inside one bounded
    Worker turn. The existing Run model has no native sub-call ledger, so the
    protected Worker product counts as one logical call and the driver's
    numeric ``num_turns`` metadata feeds the Agent-step view. This is
    accounting only; Worker output is never inspected here.
    """

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
                tool_call_count=SolveRun.tool_call_count + 1,
                run_total_logical_tool_calls=SolveRun.run_total_logical_tool_calls + 1,
                attempt_logical_tool_calls=SolveRun.attempt_logical_tool_calls + 1,
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

    # Lightweight test doubles and replay callers do not have a SQLAlchemy
    # identity; retain the same projection semantics for them.
    _increment(run, "tool_call_count", 1)
    _increment(run, "run_total_logical_tool_calls", 1)
    _increment(run, "attempt_logical_tool_calls", 1)
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
