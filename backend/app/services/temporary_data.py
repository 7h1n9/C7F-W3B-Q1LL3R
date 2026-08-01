"""Run-scoped ephemeral storage and deterministic cleanup.

Only ``workspace/runtime`` is managed here.  Formal ``evidence``, ``final``,
``outputs``, and ``reports`` paths are never recursive-cleanup targets.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DomainError
from app.models.multi_agent import AgentTask
from app.models.run import (
    Artifact,
    CleanupManifest,
    EvidenceSnapshot,
    RunExecutionLease,
    SolveRun,
    ToolCall,
)
from app.orchestration.state_machine import TERMINAL

RUNTIME_ROOT = "runtime"
RUNTIME_GROUPS = (
    "agents",
    "web-research",
    "tool-subrequests",
    "streams",
    "runner-jobs",
    "pending-promotion",
    "cleanup-manifests",
)
PROTECTED_RUNTIME_PREFIXES = frozenset({"protected", "promoted", "flag-source", "fresh-reproduction"})
TASK_CLEANUP_DELAY = timedelta(minutes=5)
FAILED_RETENTION = timedelta(minutes=60)
DEBUG_RETENTION = timedelta(hours=24)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode()


class TemporaryWorkspace:
    def root(self, workspace: Path) -> Path:
        root = workspace.resolve() / RUNTIME_ROOT
        if root == workspace.resolve() or workspace.resolve() not in root.parents:
            raise DomainError("TEMPORARY_WORKSPACE_INVALID", "Runtime directory is outside the Run Workspace.")
        return root

    def ensure_layout(self, workspace: Path) -> Path:
        root = self.root(workspace)
        root.mkdir(parents=True, exist_ok=True)
        for group in RUNTIME_GROUPS:
            (root / group).mkdir(parents=True, exist_ok=True)
        return root

    def task_path(self, workspace: Path, role: str, task_id: str) -> Path:
        if not role or not task_id or any(part in {".", ".."} or "/" in part or "\\" in part for part in (role, task_id)):
            raise DomainError("TEMPORARY_PATH_INVALID", "Role and task ID must be safe path components.")
        root = self.ensure_layout(workspace)
        path = (root / "agents" / role.lower() / task_id).resolve()
        if root not in path.parents:
            raise DomainError("TEMPORARY_PATH_INVALID", "Task runtime path escapes runtime/.")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def web_path(self, workspace: Path, task_id: str) -> Path:
        root = self.ensure_layout(workspace)
        path = (root / "web-research" / task_id).resolve()
        if root not in path.parents:
            raise DomainError("TEMPORARY_PATH_INVALID", "Web research path escapes runtime/.")
        path.mkdir(parents=True, exist_ok=True)
        return path


temporary_workspace = TemporaryWorkspace()


class TemporaryDataJanitor:
    def __init__(self, *, debug_mode: bool = False) -> None:
        self.debug_mode = debug_mode

    @staticmethod
    def _files(root: Path) -> list[tuple[Path, dict[str, Any]]]:
        if not root.exists() or root.is_symlink():
            return []
        rows: list[tuple[Path, dict[str, Any]]] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            raw = path.read_bytes()
            rows.append(
                (
                    path,
                    {
                        "path": path.relative_to(root).as_posix(),
                        "size": len(raw),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    },
                )
            )
        return rows

    @staticmethod
    def _protected(relative: str) -> bool:
        first = relative.split("/", 1)[0]
        return first in PROTECTED_RUNTIME_PREFIXES

    def _archive(self, workspace: Path, rows: list[tuple[Path, dict[str, Any]]], archive_name: str) -> tuple[str, str]:
        archive_root = workspace.resolve() / "archive" / "temporary"
        archive_root.mkdir(parents=True, exist_ok=True)
        archive = (archive_root / archive_name).resolve()
        if archive_root.resolve() not in archive.parents:
            raise DomainError("TEMPORARY_ARCHIVE_INVALID", "Temporary archive path is invalid.")
        with gzip.open(archive, "wt", encoding="utf-8") as handle:
            for path, metadata in rows:
                record = dict(metadata)
                record["content_base64"] = base64.b64encode(path.read_bytes()).decode("ascii")
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return archive.relative_to(workspace.resolve()).as_posix(), hashlib.sha256(archive.read_bytes()).hexdigest()

    @staticmethod
    def _remove_tree(path: Path) -> None:
        resolved = path.resolve()
        if resolved.name in {"", ".", ".."} or not resolved.exists() or resolved.is_symlink():
            return
        if resolved.is_file():
            resolved.unlink()
            return
        shutil.rmtree(resolved)

    async def _manifest(
        self,
        session: AsyncSession,
        *,
        run: SolveRun,
        kind: str,
        key: str,
        rows: list[tuple[Path, dict[str, Any]]],
        task_id: str | None,
        archive_path: str | None,
        archive_sha256: str | None,
        retention_deadline: datetime | None,
        deleted: list[str],
        preserved: list[str],
    ) -> CleanupManifest:
        existing = await session.scalar(select(CleanupManifest).where(CleanupManifest.idempotency_key == key))
        if existing is not None:
            return existing
        manifest = {"rows": [metadata for _, metadata in rows], "row_count": len(rows), "created_at": datetime.now(UTC).isoformat(), "retention_deadline": retention_deadline.isoformat() if retention_deadline else None, "run_id": run.id, "agent_task_id": task_id}
        item = CleanupManifest(
            run_id=run.id,
            agent_task_id=task_id,
            cleanup_kind=kind,
            status="COMPLETED",
            idempotency_key=key,
            manifest_json=manifest,
            sha256=hashlib.sha256(_canonical(manifest)).hexdigest(),
            row_count=len(rows),
            archive_path=archive_path,
            archive_sha256=archive_sha256,
            retention_deadline=retention_deadline,
            completed_at=datetime.now(UTC),
            deleted_paths_json=deleted,
            preserved_paths_json=preserved,
            debug_mode=self.debug_mode,
        )
        session.add(item)
        await session.flush()
        return item

    async def cleanup_task(self, session: AsyncSession, run: SolveRun, task: AgentTask, *, now: datetime | None = None, force: bool = False) -> dict:
        now = now or datetime.now(UTC)
        if task.status in {"PENDING", "RUNNING"} and not force:
            return {"status": "SKIPPED", "reason": "TASK_ACTIVE", "task_id": task.id}
        if task.lease_expires_at and task.lease_expires_at > now and not force:
            return {"status": "SKIPPED", "reason": "LEASE_ACTIVE", "task_id": task.id}
        workspace = Path(run.workspace_path).resolve()
        task_root = None
        if task.runtime_path:
            task_root = Path(task.runtime_path)
            if not task_root.is_absolute():
                task_root = workspace / task_root
            task_root = task_root.resolve()
        runtime_root = temporary_workspace.root(workspace)
        if task_root is None:
            task_root = (runtime_root / "agents" / task.agent_role.lower() / task.id).resolve()
        if runtime_root not in task_root.parents or task_root == runtime_root:
            raise DomainError("TEMPORARY_PATH_INVALID", "Task cleanup target is outside runtime/.")
        rows = self._files(task_root)
        preserved = [metadata["path"] for _, metadata in rows if self._protected(metadata["path"])]
        deletable = [(path, metadata) for path, metadata in rows if metadata["path"] not in preserved]
        failed = task.status in {"FAILED", "BLOCKED", "NEED_REPLAN"}
        deadline = now + (DEBUG_RETENTION if self.debug_mode else FAILED_RETENTION if failed else TASK_CLEANUP_DELAY)
        archive_path = archive_sha = None
        if failed or self.debug_mode:
            archive_path, archive_sha = self._archive(workspace, deletable, f"task-{task.id}.jsonl.gz")
        deleted: list[str] = []
        for path, metadata in deletable:
            path.unlink(missing_ok=True)
            deleted.append(metadata["path"])
        if task_root.exists() and not any(task_root.iterdir()):
            self._remove_tree(task_root)
        item = await self._manifest(session, run=run, kind="TASK", key=f"TASK:{run.id}:{task.id}", rows=rows, task_id=task.id, archive_path=archive_path, archive_sha256=archive_sha, retention_deadline=deadline, deleted=deleted, preserved=preserved)
        return {"status": item.status, "manifest_id": item.id, "task_id": task.id, "deleted": deleted, "preserved": preserved, "archive_path": archive_path}

    async def cleanup_terminal_run(self, session: AsyncSession, run: SolveRun, *, now: datetime | None = None, force: bool = False) -> dict:
        now = now or datetime.now(UTC)
        if run.status not in {item.value for item in TERMINAL}:
            return {"status": "SKIPPED", "reason": "RUN_NOT_TERMINAL", "run_id": run.id}
        existing = await session.scalar(select(CleanupManifest).where(CleanupManifest.idempotency_key == f"RUN:{run.id}:terminal"))
        if existing is not None:
            run.terminal_cleanup_completed = True
            run.terminal_cleanup_manifest_id = existing.id
            return {"status": existing.status, "manifest_id": existing.id, "run_id": run.id}
        active_calls = int(await session.scalar(select(func.count()).select_from(ToolCall).where(ToolCall.run_id == run.id, ToolCall.status.in_(["REQUESTED", "STARTED"]))) or 0)
        active_lease = await session.scalar(select(RunExecutionLease.id).where(RunExecutionLease.run_id == run.id, RunExecutionLease.expires_at > now))
        if (active_calls or active_lease) and not force:
            return {"status": "SKIPPED", "reason": "ACTIVE_DATA_PRESENT", "run_id": run.id}
        workspace = Path(run.workspace_path).resolve()
        runtime_root = temporary_workspace.root(workspace)
        rows = self._files(runtime_root) if runtime_root.exists() else []
        preserved = [metadata["path"] for _, metadata in rows if self._protected(metadata["path"])]
        deletable = [(path, metadata) for path, metadata in rows if metadata["path"] not in preserved]
        archive_path, archive_sha = self._archive(workspace, deletable, f"run-{run.id}-terminal.jsonl.gz") if deletable else (None, None)
        deleted: list[str] = []
        for path, metadata in deletable:
            path.unlink(missing_ok=True)
            deleted.append(metadata["path"])
        for directory in sorted((path for path in runtime_root.rglob("*") if path.is_dir()), reverse=True) if runtime_root.exists() else []:
            if directory.exists() and not any(directory.iterdir()):
                directory.rmdir()
        item = await self._manifest(session, run=run, kind="TERMINAL_RUN", key=f"RUN:{run.id}:terminal", rows=rows, task_id=None, archive_path=archive_path, archive_sha256=archive_sha, retention_deadline=now + DEBUG_RETENTION if self.debug_mode else now + FAILED_RETENTION, deleted=deleted, preserved=preserved)
        protected_artifact_ids = list(
            (await session.scalars(
                select(Artifact.id).where(
                    Artifact.run_id == run.id,
                    Artifact.retention_class.in_(["PROTECTED", "FINAL", "FLAG_SOURCE", "FRESH_REPRODUCTION", "REPORT_REFERENCED"]),
                )
            )).all()
        )
        snapshot_data = {"run_id": run.id, "cleanup_manifest_id": item.id, "protected_artifact_ids": [str(value) for value in protected_artifact_ids]}
        snapshot = EvidenceSnapshot(run_id=run.id, generation=run.cleanup_generation + 1, snapshot_json=snapshot_data, sha256=hashlib.sha256(_canonical(snapshot_data)).hexdigest(), is_current=True)
        session.add(snapshot)
        await session.flush()
        run.cleanup_generation += 1
        run.terminal_cleanup_completed = True
        run.terminal_cleanup_manifest_id = item.id
        run.terminal_cleanup_at = now
        run.terminal_evidence_snapshot_id = snapshot.id
        return {"status": item.status, "manifest_id": item.id, "snapshot_id": snapshot.id, "run_id": run.id, "deleted": deleted, "preserved": preserved}

    async def run_once(self, session: AsyncSession, *, now: datetime | None = None) -> dict:
        now = now or datetime.now(UTC)
        task_cleaned = 0
        terminal_cleaned = 0
        tasks = list((await session.scalars(select(AgentTask))).all())
        runs = {item.id: item for item in (await session.scalars(select(SolveRun))).all()}
        for task in tasks:
            run = runs.get(task.run_id)
            if not run:
                continue
            created_at = task.created_at.replace(tzinfo=UTC) if task.created_at.tzinfo is None else task.created_at
            due = task.status not in {"PENDING", "RUNNING"} and created_at + (DEBUG_RETENTION if self.debug_mode else FAILED_RETENTION if task.status in {"FAILED", "BLOCKED", "NEED_REPLAN"} else TASK_CLEANUP_DELAY) <= now
            if due:
                result = await self.cleanup_task(session, run, task, now=now)
                task_cleaned += int(result.get("status") == "COMPLETED")
        for run in runs.values():
            result = await self.cleanup_terminal_run(session, run, now=now)
            terminal_cleaned += int(result.get("status") == "COMPLETED")
        await session.commit()
        return {"task_cleaned": task_cleaned, "terminal_cleaned": terminal_cleaned, "at": now.isoformat()}


temporary_data_janitor = TemporaryDataJanitor()
