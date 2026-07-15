"""Run-scoped workspace authorization and manifest helpers.

The policy is deliberately independent from FastAPI and the Runner client so
both the internal ctfctl surface and background orchestration can use the same
path rules.  Paths are always slash-normalized before validation and symlink
resolution is checked against the run root.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.core.exceptions import DomainError

READABLE_ROOT_FILES = frozenset({"challenge.json", "AGENTS.md"})
READABLE_DIRECTORIES = frozenset(
    {
        "source", "attachments", "requests", "responses", "outputs", "evidence",
        "scripts", "notes", "final", "scratch", "payloads", "extracted", "generated",
    }
)
READ_ONLY_PREFIXES = frozenset({"source", "attachments"})
WRITABLE_PREFIXES = frozenset(
    {"scripts", "scratch", "payloads", "notes", "generated", "extracted", "requests/generated",
     "outputs/generated", "evidence/derived", "final/draft"}
)
DELETABLE_PREFIXES = frozenset(
    {"scratch", "payloads", "generated", "scripts/generated", "final/draft"}
)
SYNC_BACK_PREFIXES = frozenset({"outputs", "evidence", "responses", "final"})


def _relative(raw: str) -> str:
    value = str(raw or "").replace("\\", "/")
    pure = PurePosixPath(value)
    if not value or pure.is_absolute() or ".." in pure.parts or "." in pure.parts or any(":" in p for p in pure.parts):
        raise DomainError("WORKSPACE_PATH_INVALID", "Only a relative workspace path is allowed.", {"path": raw}, 422)
    return pure.as_posix()


def _under(relative: str, prefixes: frozenset[str]) -> bool:
    return any(relative == prefix or relative.startswith(prefix + "/") for prefix in prefixes)


@dataclass(frozen=True)
class WorkspacePolicy:
    run_id: str
    attempt_id: str
    lease_token: str
    workspace_root: Path

    def __post_init__(self) -> None:
        root = Path(self.workspace_root).resolve()
        object.__setattr__(self, "workspace_root", root)
        if not self.run_id or not self.attempt_id or not self.lease_token:
            raise DomainError("WORKSPACE_SCOPE_INVALID", "run_id, attempt_id and lease_token are required.", status_code=403)

    def path(self, raw: str, *, operation: str = "read", allow_missing: bool = False) -> tuple[Path, str]:
        relative = _relative(raw)
        first = relative.split("/", 1)[0]
        if operation == "read":
            if relative not in READABLE_ROOT_FILES and first not in READABLE_DIRECTORIES:
                raise DomainError("WORKSPACE_READ_FORBIDDEN", "Path is outside the readable Run Workspace.", {"path": relative}, 403)
        elif operation == "write":
            if not _under(relative, WRITABLE_PREFIXES):
                raise DomainError("WORKSPACE_WRITE_FORBIDDEN", "Path is outside the writable generated areas.", {"path": relative}, 403)
            if _under(relative, READ_ONLY_PREFIXES):
                raise DomainError("WORKSPACE_READ_ONLY", "Original challenge material is read-only.", {"path": relative}, 403)
        elif operation == "delete":
            if not _under(relative, DELETABLE_PREFIXES):
                raise DomainError("WORKSPACE_DELETE_FORBIDDEN", "Only agent-generated files may be deleted.", {"path": relative}, 403)
        else:
            raise DomainError("WORKSPACE_OPERATION_INVALID", "Unsupported workspace operation.", {"operation": operation}, 422)
        target = (self.workspace_root / relative).resolve()
        if self.workspace_root not in target.parents:
            raise DomainError("WORKSPACE_PATH_INVALID", "Path escapes the Run Workspace.", {"path": relative}, 422)
        if not allow_missing and operation == "read" and not target.exists():
            raise DomainError("FILE_NOT_FOUND", "Requested workspace file does not exist.", {"path": relative}, 404)
        return target, relative

    def readable(self, raw: str) -> tuple[Path, str]:
        return self.path(raw, operation="read")

    def writable(self, raw: str) -> tuple[Path, str]:
        return self.path(raw, operation="write", allow_missing=True)

    def deletable(self, raw: str) -> tuple[Path, str]:
        return self.path(raw, operation="delete")

    def can_read_directory(self, raw: str) -> bool:
        relative = str(raw or "").replace("\\", "/").strip("/")
        return relative == "" or relative in READABLE_DIRECTORIES


def file_manifest(root: Path, *, source: str = "backend_workspace", prefixes: frozenset[str] | None = None) -> list[dict]:
    root = root.resolve()
    if not root.exists():
        return []
    entries: list[dict] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if prefixes and not _under(relative, prefixes):
            continue
        raw = path.read_bytes()
        stat = path.stat()
        entries.append({
            "relative_path": relative,
            "path": relative,
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "modified_at": stat.st_mtime,
            "source": source,
            "backend_present": source == "backend_workspace",
            "runner_present": source == "runner_workspace",
        })
    return entries


def ensure_inside(root: Path, candidate: Path) -> Path:
    resolved = candidate.resolve()
    if resolved != root.resolve() and root.resolve() not in resolved.parents:
        raise DomainError("WORKSPACE_PATH_INVALID", "Path escapes the Run Workspace.", status_code=422)
    return resolved
