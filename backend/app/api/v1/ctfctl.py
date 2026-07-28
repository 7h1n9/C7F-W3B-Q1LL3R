"""Private, run-scoped backend surface for the ctfctl MCP subprocess."""
from __future__ import annotations

import hashlib
import asyncio
import secrets
import shutil
import tarfile
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_session
from app.core.exceptions import DomainError
from app.models.challenge import Challenge
from app.models.multi_agent import AgentTask
from app.models.run import AgentTurn, RunAttempt, RunExecutionLease, SolveRun, ToolInvocationTicket
from app.orchestration.state_machine import RunStatus, transition
from app.services.events import event_service
from app.services.tool_permissions import effective_tools_for
from app.services.workspace_policy import (
    READABLE_DIRECTORIES,
    READABLE_ROOT_FILES,
    WorkspacePolicy,
    file_manifest,
    searchable_path,
)
from app.services.workspace_sync import workspace_sync_service
from app.services.solver_state import solver_state_service
from app.tools.gateway import tool_gateway
from app.tools.registry import load_tool_definitions

router = APIRouter(prefix="/internal/ctfctl", tags=["internal-ctfctl"])
_workspace_locks: dict[str, asyncio.Lock] = {}


class Scope(BaseModel):
    run_id: str
    challenge_id: str
    workspace_root: str
    allowed_hosts: list[str] = Field(default_factory=list)
    attempt_id: str
    lease_token: str
    master_lease_token: str | None = None
    thread_id: str | None = None
    model_turn_id: str | None = None
    logical_tool_call_id: str | None = None
    turn_id: str | None = None
    turn_started_at: str | None = None
    agent_task_id: str | None = None
    agent_role: str | None = None
    task_lease_token: str | None = None
    allowed_tools: list[str] = Field(default_factory=list)


class Request(BaseModel):
    scope: Scope
    tool_ticket: str | None = None
    # Set only by the backend fallback controller.  It gives the required
    # CREATE/VALIDATE/EXECUTE chain its independent reservation pool.
    required_action: bool = False
    required_action_kind: str | None = None


class ReadRequest(Request):
    path: str
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    max_chars: int = Field(default=12000, ge=1, le=50000)


class SearchRequest(Request):
    query: str = Field(min_length=1, max_length=500)
    max_results: int = Field(default=50, ge=1, le=200)


class WriteRequest(Request):
    path: str
    content: str = Field(max_length=200000)
    encoding: str = "utf-8"
    overwrite: bool = False


class PatchRequest(WriteRequest):
    old_text: str | None = None
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class MkdirRequest(Request):
    path: str


class CopyRequest(Request):
    source: str
    destination: str
    overwrite: bool = False


class MoveGeneratedRequest(Request):
    source: str
    destination: str


class DeleteGeneratedRequest(Request):
    path: str


class ExtractArchiveRequest(Request):
    path: str
    max_files: int = Field(default=500, ge=1, le=5000)
    max_bytes: int = Field(default=50_000_000, ge=1, le=200_000_000)


class InvokeRequest(Request):
    tool: str
    arguments: dict


async def _workspace_logical_call(session: AsyncSession, run: SolveRun, payload: Request, tool_name: str) -> None:
    """Record a Workspace MCP action when no inner ToolGateway exists."""
    from app.services.effective_logical_tool_calls import effective_logical_tool_call_service
    from app.services.run_budget_guard import run_budget_guard

    lock = _workspace_locks.setdefault(run.id, asyncio.Lock())
    if lock.locked():
        raise DomainError("WORKSPACE_CONCURRENT_ACCESS", "Workspace MCP operations are serialized per Run.", {"max_concurrency": 1}, 409)
    await lock.acquire()
    try:
        state = await solver_state_service.load(session, run.id)
        if tool_name == "workspace_read" and isinstance(payload, ReadRequest) and state:
            relative = str(payload.path).replace("\\", "/")
            end_line = payload.end_line or 0
            if any(item.get("path") == relative and int(item.get("start_line", 1)) == payload.start_line and (int(item.get("end_line", 0)) == end_line or end_line == 0) for item in (state.read_ranges_json or [])):
                raise DomainError("EVIDENCE_ALREADY_AVAILABLE", "This workspace range is already present in the Evidence Ledger.", {"path": relative, "counts_toward_budget": False, "new_logical_tool_call": False}, 409)
            if str(run.current_phase or "") == "FLAG_SEARCH" and state.read_ranges_json:
                raise DomainError("METHOD_ACTION_REQUIRED", "FLAG_SEARCH permits one workspace read; continue with the required bounded action.", {"path": relative, "counts_toward_budget": False}, 409)

        # Validate file existence before creating or reserving a logical call.
        if tool_name in {"workspace_read", "workspace_stat"} and isinstance(payload, ReadRequest):
            policy = policy_for(payload, Path(run.workspace_path).resolve())
            relative = str(payload.path).replace("\\", "/")
            cache_key = f"{run.workspace_revision}:{relative}"
            negative = dict(run.workspace_negative_cache_json or {})
            if cache_key in negative:
                raise DomainError("FILE_NOT_FOUND_CACHED", "Requested workspace path does not exist.", {"path": relative, "counts_toward_budget": False}, 404)
            target, normalized = policy.path(payload.path, operation="read", allow_missing=True)
            if not target.is_file():
                negative[cache_key] = {"path": normalized, "workspace_revision": run.workspace_revision}
                run.workspace_negative_cache_json = negative
                await session.commit()
                raise DomainError("FILE_NOT_FOUND", "Requested workspace path does not exist.", {"path": normalized, "counts_toward_budget": False}, 404)
        turn_id = run.active_turn_id or payload.scope.turn_id or payload.scope.model_turn_id
        turn_started_at = await session.scalar(select(AgentTurn.turn_started_at).where(AgentTurn.id == turn_id, AgentTurn.run_id == run.id)) if turn_id else None
        logical_id = payload.scope.logical_tool_call_id
        if not logical_id:
            logical_id = effective_logical_tool_call_service.build_mcp_id(
                run.id, payload.scope.attempt_id, str(turn_id or "turn"), str(uuid.uuid4())
            )
        await run_budget_guard.enforce(session, run, attempt_id=payload.scope.attempt_id, turn_id=turn_id, required_action=bool(payload.required_action), required_action_kind=payload.required_action_kind or tool_name)
        logical = await effective_logical_tool_call_service.ensure(
            session, run, logical_tool_call_id=logical_id, tool_name=tool_name, arguments={}, status="COMPLETED",
            attempt_id=payload.scope.attempt_id, counts_toward_budget=True, logical_kind="WORKSPACE_MCP",
            provider_tool_name=f"ctfctl.{tool_name}", effective_tool_name=tool_name, turn_id=turn_id, turn_started_at=turn_started_at,
        )
        await run_budget_guard.release(session, run, turn_id=turn_id, required_action=bool(payload.required_action), required_action_kind=payload.required_action_kind or tool_name)
        await effective_logical_tool_call_service.trace(session, logical, execution_layer="ctfctl", event_type="completed", external_id=payload.scope.logical_tool_call_id or logical_id)
        await session.commit()
    finally:
        lock.release()


async def _workspace_changed(session: AsyncSession, run: SolveRun) -> None:
    run.workspace_revision = int(run.workspace_revision or 0) + 1
    run.workspace_negative_cache_json = {}
    await session.commit()


class DirectToolRequest(Request):
    model_config = ConfigDict(extra="allow")

class ToolTicketRequest(BaseModel):
    run_id: str
    thread_id: str | None = None
    current_attempt_id: str
    model_turn_id: str | None = None


async def scoped_run(payload: Request, access_key: str | None, session: AsyncSession) -> tuple[SolveRun, Challenge, Path]:
    if not access_key or access_key != get_settings().ctfctl_internal_access_key:
        raise DomainError("CTFCTL_UNAUTHORIZED", "Invalid ctfctl internal credential.", status_code=401)
    run = await session.get(SolveRun, payload.scope.run_id)
    if not run or run.challenge_id != payload.scope.challenge_id:
        raise DomainError("CTFCTL_SCOPE_INVALID", "Run scope is invalid.", status_code=403)
    root = Path(run.workspace_path).resolve()
    if str(root) != str(Path(payload.scope.workspace_root).resolve()):
        raise DomainError("CTFCTL_SCOPE_INVALID", "Workspace scope does not match the run.", status_code=403)
    challenge = await session.get(Challenge, run.challenge_id)
    if not challenge or sorted(payload.scope.allowed_hosts) != sorted(challenge.allowed_hosts or []):
        raise DomainError("CTFCTL_SCOPE_INVALID", "Allowed host scope does not match the run.", status_code=403)
    lease = await session.scalar(select(RunExecutionLease).where(RunExecutionLease.run_id == run.id))
    attempt = await session.get(RunAttempt, payload.scope.attempt_id)
    now = datetime.now(UTC)
    expires_at = lease.expires_at.replace(tzinfo=UTC) if lease and lease.expires_at.tzinfo is None else lease.expires_at if lease else None
    master_token = payload.scope.master_lease_token or payload.scope.lease_token
    presented_ticket = payload.tool_ticket
    master_lease = bool(not presented_ticket and lease and lease.lease_token == master_token)
    ticket = None
    ticket_valid = False
    if not master_lease:
        ticket = await session.scalar(
            select(ToolInvocationTicket).where(
                ToolInvocationTicket.ticket_hash == hashlib.sha256(presented_ticket.encode()).hexdigest() if presented_ticket else False,
                ToolInvocationTicket.run_id == run.id,
                ToolInvocationTicket.attempt_id == attempt.id if attempt else False,
                ToolInvocationTicket.lease_id == lease.id if lease else False,
            )
        )
        ticket_expires = ticket.expires_at.replace(tzinfo=UTC) if ticket and ticket.expires_at and ticket.expires_at.tzinfo is None else ticket.expires_at if ticket else None
        if ticket and ticket_expires and ticket_expires > now:
            consumed = await session.execute(
                update(ToolInvocationTicket)
                .where(
                    ToolInvocationTicket.id == ticket.id,
                    ToolInvocationTicket.used_at.is_(None),
                    ToolInvocationTicket.expires_at > now,
                )
                .values(used_at=now)
                .execution_options(synchronize_session=False)
            )
            ticket_valid = consumed.rowcount == 1
    if (
        not lease
        or not attempt
        or (not master_lease and not ticket_valid)
        or lease.attempt_id != payload.scope.attempt_id
        or attempt.run_id != run.id
        or attempt.status != "RUNNING"
        or (expires_at and expires_at <= now)
    ):
        raise DomainError("CTFCTL_LEASE_INVALID", "Thread execution lease is no longer active.", {"attempt_id": payload.scope.attempt_id}, 409)
    if payload.scope.agent_task_id:
        task = await session.get(AgentTask, payload.scope.agent_task_id)
        if (
            not task
            or task.run_id != run.id
            or task.status != "RUNNING"
            or task.lease_token != payload.scope.task_lease_token
            or (payload.scope.agent_role and task.agent_role != payload.scope.agent_role)
        ):
            raise DomainError("AGENT_SCOPE_INVALID", "The ctfctl scope is not owned by the running AgentTask.", {"agent_task_id": payload.scope.agent_task_id}, 403)
        if payload.scope.allowed_tools and sorted(payload.scope.allowed_tools) != sorted(task.allowed_tools_json or []):
            raise DomainError("AGENT_SCOPE_INVALID", "The ctfctl tool allowlist does not match the AgentTask contract.", {"agent_task_id": task.id}, 403)
    if ticket_valid:
        await session.commit()
    return run, challenge, root

@router.post("/tool-ticket")
async def tool_ticket(payload: ToolTicketRequest, x_ctfctl_access_key: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> dict:
    if not x_ctfctl_access_key or x_ctfctl_access_key != get_settings().ctfctl_internal_access_key:
        raise DomainError("CTFCTL_UNAUTHORIZED", "Invalid ctfctl internal credential.", status_code=401)
    run = await session.get(SolveRun, payload.run_id)
    attempt = await session.get(RunAttempt, payload.current_attempt_id)
    lease = await session.scalar(select(RunExecutionLease).where(RunExecutionLease.run_id == payload.run_id))
    now = datetime.now(UTC)
    if not run or not attempt or attempt.run_id != payload.run_id or attempt.status != "RUNNING" or not lease or lease.attempt_id != attempt.id:
        raise DomainError("CTFCTL_LEASE_INVALID", "Run, attempt, or lease is not active.", status_code=409)
    raw_ticket = secrets.token_urlsafe(32)
    lease_expires = lease.expires_at.replace(tzinfo=UTC) if lease.expires_at.tzinfo is None else lease.expires_at
    ticket = ToolInvocationTicket(
        ticket_hash=hashlib.sha256(raw_ticket.encode()).hexdigest(),
        run_id=run.id,
        attempt_id=attempt.id,
        thread_id=payload.thread_id,
        model_turn_id=payload.model_turn_id,
        lease_id=lease.id,
        created_at=now,
        expires_at=min(lease_expires, now + timedelta(seconds=60)),
    )
    session.add(ticket)
    await session.commit()
    expires_at = ticket.expires_at.replace(tzinfo=UTC) if ticket.expires_at.tzinfo is None else ticket.expires_at
    return {"data": {"run_id": run.id, "attempt_id": attempt.id, "ticket": raw_ticket, "expires_at": expires_at.isoformat(), "ttl_seconds": 60}}


def policy_for(payload: Request, root: Path) -> WorkspacePolicy:
    return WorkspacePolicy(payload.scope.run_id, payload.scope.attempt_id, payload.scope.lease_token, root)


def safe_path(root: Path, raw: str) -> tuple[Path, str]:
    pure = PurePosixPath(raw.replace("\\", "/"))
    if pure.is_absolute() or ".." in pure.parts or str(pure) in {"", "."}:
        raise DomainError("WORKSPACE_PATH_INVALID", "Only a relative workspace path is allowed.", status_code=422)
    target = (root / pure).resolve()
    if root not in target.parents:
        raise DomainError("WORKSPACE_PATH_INVALID", "Path escapes the current workspace.", status_code=422)
    return target, pure.as_posix()


def manifest(root: Path) -> list[dict]:
    return [
        item
        for item in file_manifest(root)
        if item["relative_path"] in READABLE_ROOT_FILES
        or item["relative_path"].split("/", 1)[0] in READABLE_DIRECTORIES
    ]


@router.post("/workspace_list")
async def workspace_list(payload: Request, x_ctfctl_access_key: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> dict:
    run, _, root = await scoped_run(payload, x_ctfctl_access_key, session)
    await _workspace_logical_call(session, run, payload, "workspace_list")
    return {"data": {"files": manifest(root)}}


@router.post("/workspace_tree")
async def workspace_tree(payload: Request, x_ctfctl_access_key: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> dict:
    run, _, root = await scoped_run(payload, x_ctfctl_access_key, session)
    await _workspace_logical_call(session, run, payload, "workspace_tree")
    return {"data": {"directories": sorted(READABLE_DIRECTORIES), "root_files": sorted(READABLE_ROOT_FILES), "files": manifest(root)}}


@router.post("/workspace_stat")
async def workspace_stat(payload: ReadRequest, x_ctfctl_access_key: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> dict:
    run, _, root = await scoped_run(payload, x_ctfctl_access_key, session)
    await _workspace_logical_call(session, run, payload, "workspace_stat")
    target, relative = policy_for(payload, root).readable(payload.path)
    stat = target.stat()
    raw = target.read_bytes() if target.is_file() else b""
    return {"data": {"relative_path": relative, "size": stat.st_size, "modified_at": stat.st_mtime, "sha256": hashlib.sha256(raw).hexdigest() if target.is_file() else None, "is_file": target.is_file(), "is_dir": target.is_dir()}}


@router.post("/workspace_read")
async def workspace_read(payload: ReadRequest, x_ctfctl_access_key: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> dict:
    run, _, root = await scoped_run(payload, x_ctfctl_access_key, session)
    await _workspace_logical_call(session, run, payload, "workspace_read")
    target, relative = policy_for(payload, root).readable(payload.path)
    if not target.is_file():
        raise DomainError("FILE_NOT_FOUND", "Requested workspace path is not a file.", {"path": relative}, 404)
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    end = payload.end_line or len(lines)
    text = "\n".join(lines[payload.start_line - 1 : end])[: payload.max_chars]
    await solver_state_service.record_file_read(
        session,
        run.id,
        path=relative,
        start_line=payload.start_line,
        end_line=min(end, len(lines)),
        content_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
    )
    return {"data": {"path": relative, "start_line": payload.start_line, "end_line": min(end, len(lines)), "content": text, "content_sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "truncated": len(text) >= payload.max_chars}}


@router.post("/workspace_search")
async def workspace_search(payload: SearchRequest, x_ctfctl_access_key: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> dict:
    run, _, root = await scoped_run(payload, x_ctfctl_access_key, session)
    await _workspace_logical_call(session, run, payload, "workspace_search")
    matches = []
    policy = policy_for(payload, root)
    for item in manifest(root):
        # Generated response/runner logs are already available through their
        # explicit artifact paths. Excluding them from broad searches keeps a
        # Codex context bounded and prevents repeated flag-pattern matches from
        # causing compaction churn during unattended runs.
        if not searchable_path(item["relative_path"]):
            continue
        if len(matches) >= payload.max_results:
            break
        path, _ = policy.readable(item["relative_path"])
        try:
            for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if payload.query.lower() in line.lower():
                    matches.append({"path": item["relative_path"], "line": number, "content": line[:1000]})
                    if len(matches) >= payload.max_results:
                        break
        except OSError:
            continue
    return {"data": {"matches": matches}}


@router.post("/workspace_write_note")
async def workspace_write_note(payload: WriteRequest, x_ctfctl_access_key: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write_workspace_file(payload, x_ctfctl_access_key, session, "workspace_write_note")


async def _write_workspace_file(payload: WriteRequest, x_ctfctl_access_key: str | None, session: AsyncSession, tool_name: str) -> dict:
    run, _, root = await scoped_run(payload, x_ctfctl_access_key, session)
    await _workspace_logical_call(session, run, payload, tool_name)
    target, relative = policy_for(payload, root).writable(payload.path)
    if target.exists() and not payload.overwrite:
        raise DomainError("FILE_EXISTS", "Destination exists and overwrite=false.", {"path": relative}, 409)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload.content, encoding=payload.encoding)
    await _workspace_changed(session, run)
    synced = True
    try:
        await workspace_sync_service.sync_to_runner(payload.scope.run_id, root)
    except Exception:
        synced = False
    raw = target.read_bytes()
    return {"data": {"relative_path": relative, "path": relative, "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw), "runner_synced": synced}}


@router.post("/workspace_write_file")
async def workspace_write_file(payload: WriteRequest, x_ctfctl_access_key: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> dict:
    return await _write_workspace_file(payload, x_ctfctl_access_key, session, "workspace_write_file")


@router.post("/workspace_patch_file")
async def workspace_patch_file(payload: PatchRequest, x_ctfctl_access_key: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> dict:
    run, _, root = await scoped_run(payload, x_ctfctl_access_key, session)
    await _workspace_logical_call(session, run, payload, "workspace_patch_file")
    policy = policy_for(payload, root)
    target, relative = policy.writable(payload.path)
    if not target.is_file():
        raise DomainError("FILE_NOT_FOUND", "Patch target does not exist.", {"path": relative}, 404)
    original = target.read_text(encoding=payload.encoding)
    if payload.old_text is not None:
        if original.count(payload.old_text) != 1:
            raise DomainError("PATCH_NOT_UNIQUE", "old_text must match exactly once.", {"path": relative}, 422)
        updated = original.replace(payload.old_text, payload.content, 1)
    elif payload.start_line is not None and payload.end_line is not None:
        lines = original.splitlines(keepends=True)
        if payload.end_line < payload.start_line or payload.end_line > len(lines):
            raise DomainError("PATCH_RANGE_INVALID", "Patch line range is invalid.", {"path": relative}, 422)
        updated = "".join([*lines[: payload.start_line - 1], payload.content, *lines[payload.end_line :]])
    else:
        raise DomainError("PATCH_INVALID", "Provide old_text or start_line/end_line.", status_code=422)
    target.write_text(updated, encoding=payload.encoding)
    await _workspace_changed(session, run)
    await workspace_sync_service.sync_to_runner(payload.scope.run_id, root)
    raw = target.read_bytes()
    return {"data": {"relative_path": relative, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "runner_synced": True}}


@router.post("/workspace_mkdir")
async def workspace_mkdir(payload: MkdirRequest, x_ctfctl_access_key: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> dict:
    run, _, root = await scoped_run(payload, x_ctfctl_access_key, session)
    await _workspace_logical_call(session, run, payload, "workspace_mkdir")
    target, relative = policy_for(payload, root).writable(payload.path)
    target.mkdir(parents=True, exist_ok=True)
    await _workspace_changed(session, run)
    return {"data": {"relative_path": relative, "created": True}}


@router.post("/workspace_copy")
async def workspace_copy(payload: CopyRequest, x_ctfctl_access_key: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> dict:
    run, _, root = await scoped_run(payload, x_ctfctl_access_key, session)
    await _workspace_logical_call(session, run, payload, "workspace_copy")
    policy = policy_for(payload, root)
    source, source_relative = policy.readable(payload.source)
    destination, destination_relative = policy.writable(payload.destination)
    if not source.is_file():
        raise DomainError("FILE_NOT_FOUND", "Copy source does not exist.", {"path": source_relative}, 404)
    if destination.exists() and not payload.overwrite:
        raise DomainError("FILE_EXISTS", "Destination exists and overwrite=false.", {"path": destination_relative}, 409)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    await _workspace_changed(session, run)
    await workspace_sync_service.sync_to_runner(payload.scope.run_id, root)
    raw = destination.read_bytes()
    return {"data": {"relative_path": destination_relative, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "runner_synced": True}}


@router.post("/workspace_move_generated")
async def workspace_move_generated(payload: MoveGeneratedRequest, x_ctfctl_access_key: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> dict:
    run, _, root = await scoped_run(payload, x_ctfctl_access_key, session)
    await _workspace_logical_call(session, run, payload, "workspace_move_generated")
    policy = policy_for(payload, root)
    source, source_relative = policy.deletable(payload.source)
    destination_relative = str(payload.destination).replace("\\", "/")
    destination, _ = policy.writable("final/draft/" + destination_relative.lstrip("/"))
    if not source.is_file():
        raise DomainError("FILE_NOT_FOUND", "Generated source does not exist.", {"path": source_relative}, 404)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))
    await _workspace_changed(session, run)
    await workspace_sync_service.sync_to_runner(payload.scope.run_id, root)
    return {"data": {"source": source_relative, "destination": destination.relative_to(root).as_posix(), "runner_synced": True}}


@router.post("/workspace_delete_generated")
async def workspace_delete_generated(payload: DeleteGeneratedRequest, x_ctfctl_access_key: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> dict:
    run, _, root = await scoped_run(payload, x_ctfctl_access_key, session)
    await _workspace_logical_call(session, run, payload, "workspace_delete_generated")
    target, relative = policy_for(payload, root).deletable(payload.path)
    if target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)
    else:
        raise DomainError("FILE_NOT_FOUND", "Generated path does not exist.", {"path": relative}, 404)
    await _workspace_changed(session, run)
    await workspace_sync_service.sync_to_runner(payload.scope.run_id, root)
    return {"data": {"relative_path": relative, "deleted": True}}


@router.post("/workspace_extract_archive")
async def workspace_extract_archive(payload: ExtractArchiveRequest, x_ctfctl_access_key: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> dict:
    run, _, root = await scoped_run(payload, x_ctfctl_access_key, session)
    await _workspace_logical_call(session, run, payload, "workspace_extract_archive")
    policy = policy_for(payload, root)
    source, relative = policy.readable(payload.path)
    if not (relative.startswith("attachments/") or relative.startswith("scratch/")):
        raise DomainError("ARCHIVE_SOURCE_FORBIDDEN", "Archives may only be read from attachments/ or scratch/.", {"path": relative}, 403)
    if not source.is_file():
        raise DomainError("FILE_NOT_FOUND", "Archive does not exist.", {"path": relative}, 404)
    output = root / "extracted" / Path(relative).stem
    output.mkdir(parents=True, exist_ok=True)
    count = total = 0
    names: list[str] = []
    def safe_member(name: str) -> Path:
        pure = PurePosixPath(name.replace("\\", "/"))
        if pure.is_absolute() or ".." in pure.parts or "." in pure.parts or any(":" in p for p in pure.parts):
            raise DomainError("ARCHIVE_PATH_INVALID", "Archive contains an absolute or traversal path.", {"member": name}, 422)
        destination = (output / pure).resolve()
        if output.resolve() not in destination.parents:
            raise DomainError("ARCHIVE_PATH_INVALID", "Archive member escapes extracted/.", {"member": name}, 422)
        return destination
    try:
        if zipfile.is_zipfile(source):
            with zipfile.ZipFile(source) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    if (info.external_attr >> 16) & 0o170000 == 0o120000:
                        raise DomainError("ARCHIVE_LINK_FORBIDDEN", "Archive links are not allowed.", {"member": info.filename}, 422)
                    if count >= payload.max_files or total + info.file_size > payload.max_bytes:
                        raise DomainError("ARCHIVE_LIMIT_EXCEEDED", "Archive extraction limits exceeded.", status_code=413)
                    destination = safe_member(info.filename)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(archive.read(info))
                    count += 1; total += info.file_size; names.append(destination.relative_to(root).as_posix())
        else:
            with tarfile.open(source) as archive:
                for info in archive.getmembers():
                    if info.isdir():
                        continue
                    if info.issym() or info.islnk():
                        raise DomainError("ARCHIVE_LINK_FORBIDDEN", "Archive links are not allowed.", {"member": info.name}, 422)
                    if count >= payload.max_files or total + info.size > payload.max_bytes:
                        raise DomainError("ARCHIVE_LIMIT_EXCEEDED", "Archive extraction limits exceeded.", status_code=413)
                    destination = safe_member(info.name)
                    handle = archive.extractfile(info)
                    if handle is None:
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True); destination.write_bytes(handle.read())
                    count += 1; total += info.size; names.append(destination.relative_to(root).as_posix())
    except (zipfile.BadZipFile, tarfile.TarError) as error:
        raise DomainError("ARCHIVE_INVALID", "Unsupported or invalid archive.", {"error": str(error)}, 422) from error
    await workspace_sync_service.sync_to_runner(payload.scope.run_id, root)
    await _workspace_changed(session, run)
    return {"data": {"extracted": names, "file_count": count, "total_bytes": total, "runner_synced": True}}


@router.post("/list_tools")
async def list_tools(payload: Request, x_ctfctl_access_key: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> dict:
    run, challenge, _ = await scoped_run(payload, x_ctfctl_access_key, session)
    definitions = load_tool_definitions()
    allowed = await effective_tools_for(session, run, challenge)
    workspace_tools = {
        "workspace_list", "workspace_tree", "workspace_stat", "workspace_read", "workspace_search",
        "workspace_write_file", "workspace_write_note", "workspace_patch_file", "workspace_mkdir", "workspace_copy",
        "workspace_move_generated", "workspace_delete_generated", "workspace_extract_archive",
    }
    workspace_parameters = {
        "workspace_list": {"type": "object", "properties": {}, "additionalProperties": False},
        "workspace_tree": {"type": "object", "properties": {}, "additionalProperties": False},
        "workspace_stat": {"path": {"type": "string", "required": True}},
        "workspace_read": {"path": {"type": "string", "required": True}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}, "max_chars": {"type": "integer"}},
        "workspace_search": {"query": {"type": "string", "required": True}, "max_results": {"type": "integer"}},
        "workspace_write_file": {"path": {"type": "string", "required": True}, "content": {"type": "string", "required": True}, "encoding": {"type": "string"}, "overwrite": {"type": "boolean"}},
        "workspace_write_note": {"path": {"type": "string", "required": True}, "content": {"type": "string", "required": True}, "encoding": {"type": "string"}, "overwrite": {"type": "boolean"}},
        "workspace_patch_file": {"path": {"type": "string", "required": True}, "content": {"type": "string", "required": True}, "old_text": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}},
    }
    rows = [{"name": name, "description": definitions[name].description, "parameters": definitions[name].parameters} for name in sorted(allowed) if name in definitions and definitions[name].enabled]
    rows.extend({"name": name, "description": "Run-scoped workspace operation.", "parameters": workspace_parameters.get(name, {})} for name in sorted(workspace_tools))
    return {"data": {"tools": rows}}


@router.post("/tool/{tool_name}")
async def direct_tool(tool_name: str, payload: DirectToolRequest, x_ctfctl_access_key: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> dict:
    if tool_name.startswith("ctfctl."):
        tool_name = tool_name.removeprefix("ctfctl.")
    if tool_name in {"workspace_list", "workspace_read", "workspace_search"}:
        raise DomainError("TOOL_INVALID_ARGUMENT", "Workspace tools use their dedicated schema endpoint.", {"tool": tool_name}, 422)
    raw = payload.model_dump(exclude={"scope"})
    # The one-shot Ticket authenticates the gateway request; it is not an
    # argument of the underlying tool. Keeping it in this dict makes the
    # declarative ToolDefinition validator reject every direct MCP tool call
    # as an unexpected field.
    raw.pop("tool_ticket", None)
    # These are backend control-plane fields, not arguments of the declared
    # tool.  Keeping them here makes every direct MCP call fail strict schema
    # validation with extra_forbidden (notably script_run).
    raw.pop("required_action", None)
    raw.pop("required_action_kind", None)
    arguments = raw.pop("arguments", None)
    if not isinstance(arguments, dict):
        arguments = raw
    return await invoke_tool(InvokeRequest(scope=payload.scope, tool=tool_name, arguments=arguments), x_ctfctl_access_key, session)


@router.post("/invoke_tool")
async def invoke_tool(payload: InvokeRequest, x_ctfctl_access_key: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> dict:
    run, challenge, _ = await scoped_run(payload, x_ctfctl_access_key, session)
    # Codex SDK reports MCP calls as tool events but does not emit a separate
    # state-transition event. A normal turn reaches PLANNING before its first
    # ctfctl.invoke_tool call, so PLANNING must be allowed to enter EXECUTING
    # through the same guarded transition path.
    if RunStatus(run.status) in {RunStatus.ANALYZING, RunStatus.EVALUATING, RunStatus.PREPARING, RunStatus.PLANNING, RunStatus.RETRYING}:
        await transition_for_tool(session, run)
    if RunStatus(run.status) not in {RunStatus.EXECUTING, RunStatus.ANALYZING, RunStatus.PLANNING, RunStatus.EVALUATING, RunStatus.RETRYING}:
        raise DomainError("RUN_TOOL_NOT_ALLOWED", "Run is not ready for a tool invocation.", {"status": run.status}, 409)
    if run.current_phase == "REPORTING" and payload.tool not in {"workspace_read", "workspace_write_file", "workspace_patch_file", "workspace_stat", "workspace_list", "workspace_tree"}:
        raise DomainError("RUN_TOOL_NOT_ALLOWED", "Only evidence and final-workspace tools are allowed during reporting.", {"status": run.status, "phase": run.current_phase}, 409)
    result = await tool_gateway.invoke(
        session,
        run,
        challenge,
        payload.tool,
        payload.arguments,
        execution_layer="multi_agent" if payload.scope.agent_task_id else "ctfctl",
        logical_tool_call_id=payload.scope.logical_tool_call_id,
        turn_id=run.active_turn_id or payload.scope.turn_id or payload.scope.model_turn_id,
        provider_tool_name=f"ctfctl.{payload.tool}",
        agent_task_id=payload.scope.agent_task_id,
        agent_role=payload.scope.agent_role,
        task_lease_token=payload.scope.task_lease_token,
    )
    if RunStatus(run.status) == RunStatus.EXECUTING:
        transition(run, RunStatus.EVALUATING)
        await session.commit()
        await event_service.append(session, run.id, "run.status_changed", {"status": run.status})
    return {"data": result}


async def transition_for_tool(session: AsyncSession, run: SolveRun) -> None:
    if RunStatus(run.status) == RunStatus.RETRYING:
        transition(run, RunStatus.PLANNING)
        await session.commit()
        await event_service.append(session, run.id, "run.status_changed", {"status": run.status})
    if RunStatus(run.status) == RunStatus.PREPARING:
        transition(run, RunStatus.ANALYZING)
        await session.commit()
        await event_service.append(session, run.id, "run.status_changed", {"status": run.status})
    if RunStatus(run.status) in {RunStatus.ANALYZING, RunStatus.EVALUATING}:
        transition(run, RunStatus.PLANNING)
        await session.commit()
        await event_service.append(session, run.id, "run.status_changed", {"status": run.status})
    transition(run, RunStatus.EXECUTING)
    await session.commit()
    await event_service.append(session, run.id, "run.status_changed", {"status": run.status})
