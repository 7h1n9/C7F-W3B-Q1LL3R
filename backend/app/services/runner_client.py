import asyncio
import hashlib
from pathlib import Path, PurePosixPath

import httpx

from app.core.config import get_settings
from app.core.exceptions import DomainError


class RunnerClient:
    def _headers(self) -> dict[str, str]:
        return {"X-Runner-Token": get_settings().runner_api_token}

    @property
    def base_url(self) -> str:
        return get_settings().runner_url.rstrip("/")

    async def health(self) -> dict:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            response = await client.get(f"{self.base_url}/health")
            response.raise_for_status()
            return response.json()

    async def capabilities(self) -> dict:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            response = await client.get(f"{self.base_url}/api/v1/capabilities", headers=self._headers())
            response.raise_for_status()
            return response.json()

    async def initialize_workspace(self, run_id: str) -> None:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            response = await client.post(
                f"{self.base_url}/api/v1/workspaces/{run_id}", headers=self._headers()
            )
            response.raise_for_status()

    async def upload_file(self, run_id: str, relative_path: str, local_path: Path) -> dict:
        pure = PurePosixPath(relative_path.replace("\\", "/"))
        if pure.is_absolute() or ".." in pure.parts or not local_path.is_file():
            raise DomainError(
                "RUNNER_UPLOAD_INVALID",
                "Only an existing relative workspace file can be uploaded.",
                status_code=422,
            )
        raw = local_path.read_bytes()
        checksum = hashlib.sha256(raw).hexdigest()
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            response = await client.put(
                f"{self.base_url}/api/v1/workspaces/{run_id}/files/{pure.as_posix()}",
                headers={**self._headers(), "X-Content-SHA256": checksum},
                content=raw,
            )
            response.raise_for_status()
            return response.json()

    async def workspace_manifest(self, run_id: str) -> dict[str, dict]:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            response = await client.get(
                f"{self.base_url}/api/v1/workspaces/{run_id}/manifest", headers=self._headers()
            )
            response.raise_for_status()
            payload = response.json()
        return {
            str(item.get("path")): item
            for item in payload.get("files", [])
            if isinstance(item, dict) and item.get("path")
        }

    async def sync_workspace(self, run_id: str, local_root: Path) -> dict:
        await self.initialize_workspace(run_id)
        remote = await self.workspace_manifest(run_id)
        candidates: list[tuple[str, Path]] = []
        for relative in ("challenge.json", "AGENTS.md"):
            path = local_root / relative
            if path.is_file():
                candidates.append((relative, path))
        for directory in ("source", "attachments", "scripts", "notes", "scratch", "payloads", "generated", "extracted", "requests", "responses", "outputs", "evidence", "final"):
            root = local_root / directory
            if root.is_dir():
                for path in root.rglob("*"):
                    if path.is_file() and not path.is_symlink():
                        candidates.append((path.relative_to(local_root).as_posix(), path))
        uploaded: list[str] = []
        for relative, path in candidates:
            checksum = hashlib.sha256(path.read_bytes()).hexdigest()
            if remote.get(relative, {}).get("sha256") != checksum:
                await self.upload_file(run_id, relative, path)
                uploaded.append(relative)
        return {"uploaded": uploaded, "remote_files": len(remote), "candidate_files": len(candidates)}

    async def create_job(
        self, run_id: str, allowed_hosts: list[str], tool: str, arguments: dict
    ) -> str:
        payload = {
            "run_id": run_id,
            "allowed_hosts": allowed_hosts,
            "tool": tool,
            "arguments": arguments,
        }
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            response = await client.post(
                f"{self.base_url}/api/v1/jobs", headers=self._headers(), json=payload
            )
            response.raise_for_status()
            return str(response.json()["job_id"])

    async def wait_job(self, job_id: str, max_wait_seconds: int = 35) -> dict:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            for _ in range(max_wait_seconds * 2):
                response = await client.get(
                    f"{self.base_url}/api/v1/jobs/{job_id}", headers=self._headers()
                )
                response.raise_for_status()
                result = response.json()
                if result["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
                    return {
                        **result.get("result", {}),
                        "job_id": result.get("job_id"),
                        "status": result["status"],
                        "error": result.get("error"),
                    }
                await asyncio.sleep(0.5)
        return {
            "job_id": job_id,
            "status": "FAILED",
            "summary": "Runner polling timed out",
            "error": "RUNNER_TIMEOUT",
        }

    async def cancel_job(self, job_id: str) -> dict:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.post(
                f"{self.base_url}/api/v1/jobs/{job_id}/cancel", headers=self._headers()
            )
            response.raise_for_status()
            return response.json()

    async def download_artifact(
        self, run_id: str, relative_path: str, destination: Path, expected_sha256: str | None = None
    ) -> tuple[int, str]:
        pure = PurePosixPath(relative_path.replace("\\", "/"))
        if pure.is_absolute() or ".." in pure.parts:
            raise DomainError(
                "RUNNER_ARTIFACT_INVALID",
                "Runner returned an invalid artifact path.",
                status_code=502,
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.download")
        hasher, size = hashlib.sha256(), 0
        try:
            async with httpx.AsyncClient(timeout=45, trust_env=False) as client:
                async with client.stream(
                    "GET",
                    f"{self.base_url}/api/v1/workspaces/{run_id}/files/{pure.as_posix()}",
                    headers=self._headers(),
                ) as response:
                    response.raise_for_status()
                    with temporary.open("wb") as handle:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            hasher.update(chunk)
                            handle.write(chunk)
                    declared = response.headers.get("X-Artifact-SHA256")
            actual = hasher.hexdigest()
            if (expected_sha256 and actual != expected_sha256) or (declared and actual != declared):
                raise DomainError(
                    "RUNNER_ARTIFACT_HASH_MISMATCH",
                    "Downloaded artifact checksum does not match Runner metadata.",
                    status_code=502,
                )
            temporary.replace(destination)
            return size, actual
        finally:
            temporary.unlink(missing_ok=True)

    async def delete_workspace(self, run_id: str) -> None:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            response = await client.delete(
                f"{self.base_url}/api/v1/workspaces/{run_id}", headers=self._headers()
            )
            response.raise_for_status()

    async def clear_sessions(self, run_id: str) -> None:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.delete(f"{self.base_url}/api/v1/sessions/{run_id}", headers=self._headers())
            response.raise_for_status()


runner_client = RunnerClient()
