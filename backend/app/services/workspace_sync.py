"""Backend/Runner manifest and incremental synchronization services."""
from __future__ import annotations

import hashlib
from pathlib import Path

from app.services.workspace_policy import SYNC_BACK_PREFIXES, file_manifest


class WorkspaceManifestService:
    def local(self, workspace_root: Path) -> list[dict]:
        return file_manifest(workspace_root, source="backend_workspace")

    def by_path(self, workspace_root: Path) -> dict[str, dict]:
        return {item["relative_path"]: item for item in self.local(workspace_root)}

    def compare(self, workspace_root: Path, runner_files: dict[str, dict]) -> list[dict]:
        backend = self.by_path(workspace_root)
        paths = sorted(set(backend) | set(runner_files))
        return [
            {
                **(backend.get(path) or runner_files.get(path) or {"relative_path": path}),
                "relative_path": path,
                "backend_present": path in backend,
                "runner_present": path in runner_files,
                "source": "both" if path in backend and path in runner_files else "backend_workspace" if path in backend else "runner_workspace",
            }
            for path in paths
        ]


class WorkspaceSyncService:
    """Coordinates one-way backend->Runner and bounded Runner->backend sync."""

    def __init__(self, client=None) -> None:
        self.client = client

    async def sync_to_runner(self, run_id: str, workspace_root: Path) -> dict:
        if self.client is None:
            from app.services.runner_client import runner_client

            self.client = runner_client
        return await self.client.sync_workspace(run_id, workspace_root)

    async def sync_from_runner(self, run_id: str, workspace_root: Path) -> list[str]:
        if self.client is None:
            from app.services.runner_client import runner_client

            self.client = runner_client
        changed: list[str] = []
        remote = await self.client.workspace_manifest(run_id)
        for relative, metadata in remote.items():
            if not any(relative == prefix or relative.startswith(prefix + "/") for prefix in SYNC_BACK_PREFIXES):
                continue
            target = workspace_root / relative
            checksum = metadata.get("sha256")
            if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == checksum:
                continue
            await self.client.download_artifact(run_id, relative, target, checksum)
            changed.append(relative)
        return changed


workspace_manifest_service = WorkspaceManifestService()
workspace_sync_service = WorkspaceSyncService()
