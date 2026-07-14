"""Private, run-scoped backend surface for the ctfctl MCP subprocess."""
from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_session
from app.core.exceptions import DomainError
from app.models.challenge import Challenge
from app.models.run import RunExecutionLease, SolveRun
from app.orchestration.state_machine import RunStatus, transition
from app.services.events import event_service
from app.services.tool_permissions import effective_tools_for
from app.tools.gateway import tool_gateway
from app.tools.registry import load_tool_definitions

router = APIRouter(prefix="/internal/ctfctl", tags=["internal-ctfctl"])


class Scope(BaseModel):
    run_id: str
    challenge_id: str
    workspace_root: str
    allowed_hosts: list[str] = Field(default_factory=list)
    attempt_id: str | None = None
    lease_token: str | None = None


class Request(BaseModel):
    scope: Scope


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


class InvokeRequest(Request):
    tool: str
    arguments: dict


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
    if payload.scope.lease_token:
        lease = await session.scalar(select(RunExecutionLease).where(RunExecutionLease.run_id == run.id))
        if not lease or lease.lease_token != payload.scope.lease_token or (
            payload.scope.attempt_id and lease.attempt_id != payload.scope.attempt_id
        ):
            raise DomainError("CTFCTL_LEASE_INVALID", "Thread execution lease is no longer active.", status_code=409)
    return run, challenge, root


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
        {"relative_path": path.relative_to(root).as_posix(), "size": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "modified_at": path.stat().st_mtime, "source": "backend_workspace"}
        for path in root.rglob("*") if path.is_file() and not path.is_symlink()
    ]


@router.post("/workspace_list")
async def workspace_list(payload: Request, x_ctfctl_access_key: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> dict:
    _, _, root = await scoped_run(payload, x_ctfctl_access_key, session)
    return {"data": {"files": manifest(root)}}


@router.post("/workspace_read")
async def workspace_read(payload: ReadRequest, x_ctfctl_access_key: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> dict:
    _, _, root = await scoped_run(payload, x_ctfctl_access_key, session)
    target, relative = safe_path(root, payload.path)
    if not target.is_file():
        raise DomainError("FILE_NOT_FOUND", "Requested workspace file does not exist.", {"path": relative, "files": [item["relative_path"] for item in manifest(root)]}, 404)
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    end = payload.end_line or len(lines)
    text = "\n".join(lines[payload.start_line - 1 : end])[: payload.max_chars]
    return {"data": {"path": relative, "start_line": payload.start_line, "end_line": min(end, len(lines)), "content": text, "content_sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "truncated": len(text) >= payload.max_chars}}


@router.post("/workspace_search")
async def workspace_search(payload: SearchRequest, x_ctfctl_access_key: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> dict:
    _, _, root = await scoped_run(payload, x_ctfctl_access_key, session)
    matches = []
    for item in manifest(root):
        if len(matches) >= payload.max_results:
            break
        path = root / item["relative_path"]
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
    _, _, root = await scoped_run(payload, x_ctfctl_access_key, session)
    target, relative = safe_path(root, payload.path)
    if not any(relative.startswith(prefix) for prefix in ("notes/", "scripts/", "final/")):
        raise DomainError("WORKSPACE_WRITE_FORBIDDEN", "ctfctl may only write notes/, scripts/, and final/.", status_code=403)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload.content, encoding="utf-8")
    return {"data": {"path": relative, "sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "size": target.stat().st_size}}


@router.post("/list_tools")
async def list_tools(payload: Request, x_ctfctl_access_key: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> dict:
    run, challenge, _ = await scoped_run(payload, x_ctfctl_access_key, session)
    definitions = load_tool_definitions()
    allowed = await effective_tools_for(session, run, challenge)
    return {"data": {"tools": [{"name": name, "description": definitions[name].description, "parameters": definitions[name].parameters} for name in sorted(allowed) if name in definitions and definitions[name].enabled]}}


@router.post("/invoke_tool")
async def invoke_tool(payload: InvokeRequest, x_ctfctl_access_key: str | None = Header(default=None), session: AsyncSession = Depends(get_session)) -> dict:
    run, challenge, _ = await scoped_run(payload, x_ctfctl_access_key, session)
    # Codex SDK reports MCP calls as tool events but does not emit a separate
    # state-transition event. A normal turn reaches PLANNING before its first
    # ctfctl.invoke_tool call, so PLANNING must be allowed to enter EXECUTING
    # through the same guarded transition path.
    if RunStatus(run.status) in {RunStatus.ANALYZING, RunStatus.EVALUATING, RunStatus.PREPARING, RunStatus.PLANNING}:
        await transition_for_tool(session, run)
    if RunStatus(run.status) != RunStatus.EXECUTING:
        raise DomainError("RUN_TOOL_NOT_ALLOWED", "Run is not ready for a tool invocation.", {"status": run.status}, 409)
    result = await tool_gateway.invoke(session, run, challenge, payload.tool, payload.arguments)
    if RunStatus(run.status) == RunStatus.EXECUTING:
        transition(run, RunStatus.EVALUATING)
        await session.commit()
        await event_service.append(session, run.id, "run.status_changed", {"status": run.status})
    return {"data": result}


async def transition_for_tool(session: AsyncSession, run: SolveRun) -> None:
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
