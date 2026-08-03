import asyncio
import hashlib
import json
import time
from pathlib import Path, PurePosixPath

import httpx

from app.core.config import get_settings
from app.core.exceptions import DomainError


def normalize_runner_job_id(response) -> str | None:
    """Normalize job identifiers across Runner API versions."""
    if isinstance(response, str):
        return response.strip() or None
    if not isinstance(response, dict):
        return None
    value = response.get("runner_job_id") or response.get("job_id") or response.get("task_id")
    return str(value).strip() if value is not None and str(value).strip() else None


class RunnerClient:
    def _headers(self) -> dict[str, str]:
        return {"X-Runner-Token": get_settings().runner_api_token}

    @property
    def base_url(self) -> str:
        return get_settings().runner_url.rstrip("/")

    @staticmethod
    def _raise_response(response: httpx.Response, *, stage: str = "RUNNER") -> None:
        if response.is_success:
            return
        try:
            body = response.json()
        except ValueError:
            body = {}
        raise DomainError(
            str(body.get("code") or "RUNNER_UNAVAILABLE"),
            str(body.get("message") or response.reason_phrase or "Runner request failed"),
            body.get("details") if isinstance(body.get("details"), dict) else {"status_code": response.status_code},
            503 if response.status_code >= 500 else response.status_code,
            stage=stage,
            retryable=response.status_code >= 500,
        )

    async def health(self) -> dict:
        # The Kali VM is reached through the host's VMware/system network
        # route. Respect that transport configuration for Runner traffic.
        async with httpx.AsyncClient(timeout=15, trust_env=True) as client:
            response = await client.get(f"{self.base_url}/health")
            response.raise_for_status()
            return response.json()

    async def capabilities(self) -> dict:
        async with httpx.AsyncClient(timeout=15, trust_env=True) as client:
            response = await client.get(f"{self.base_url}/api/v1/capabilities", headers=self._headers())
            response.raise_for_status()
            return response.json()

    async def initialize_workspace(self, run_id: str) -> None:
        async with httpx.AsyncClient(timeout=15, trust_env=True) as client:
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
        async with httpx.AsyncClient(timeout=30, trust_env=True) as client:
            response = await client.put(
                f"{self.base_url}/api/v1/workspaces/{run_id}/files/{pure.as_posix()}",
                headers={**self._headers(), "X-Content-SHA256": checksum},
                content=raw,
            )
            response.raise_for_status()
            return response.json()

    async def workspace_manifest(self, run_id: str) -> dict[str, dict]:
        async with httpx.AsyncClient(timeout=15, trust_env=True) as client:
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
        if tool == "python_run" and arguments.get("network_mode") not in (None, "none"):
            raise DomainError("PYTHON_RUN_NETWORK_FORBIDDEN", "python_run is an offline-only tool.", status_code=422)
        payload = {
            "run_id": run_id,
            "allowed_hosts": allowed_hosts,
            "tool": tool,
            "arguments": arguments,
        }
        async with httpx.AsyncClient(timeout=30, trust_env=True) as client:
            response = await client.post(
                f"{self.base_url}/api/v1/jobs", headers=self._headers(), json=payload
            )
            response.raise_for_status()
            job_id = normalize_runner_job_id(response.json())
            if not job_id:
                raise DomainError(
                    "RUNNER_JOB_ID_MISSING",
                    "Runner accepted the dispatch without returning a job identifier.",
                    stage="RUNNER_DISPATCH",
                    retryable=True,
                )
            return job_id

    async def wait_job(
        self,
        job_id: str,
        max_wait_seconds: int | None = None,
        *,
        tool_timeout_seconds: int | None = None,
    ) -> dict:
        # The Runner owns the actual timeout. Backend waiting is the tool
        # timeout plus a bounded delivery margin, never a fixed 35 seconds.
        requested = int(tool_timeout_seconds or max_wait_seconds or 30)
        deadline = time.monotonic() + min(630, max(1, requested) + 30)

        def terminal(result: dict) -> dict | None:
            if result.get("status") not in {"COMPLETED", "FAILED", "CANCELLED"}:
                return None
            payload = dict(result.get("result") or {})
            return {**payload, "job_id": result.get("job_id"), "status": payload.get("status") or result["status"], "job_status": result["status"], "error": result.get("error")}

        async with httpx.AsyncClient(timeout=15, trust_env=True) as client:
            # Poll the authoritative job record first.  The SSE endpoint may
            # keep its connection open after the job is terminal, which used
            # to prevent this method from ever reaching result collection.
            while time.monotonic() < deadline:
                try:
                    response = await client.get(f"{self.base_url}/api/v1/jobs/{job_id}", headers=self._headers())
                    self._raise_response(response)
                    done = terminal(response.json())
                    if done:
                        return done
                except (httpx.HTTPError, DomainError) as error:
                    return {"job_id": job_id, "status": "FAILED", "error_code": "RUNNER_UNAVAILABLE", "summary": "Runner became unavailable", "error": str(error), "stage": "RUNNER"}
                await asyncio.sleep(min(0.5, max(0.05, deadline - time.monotonic())))
        return {"job_id": job_id, "status": "FAILED", "error_code": "RUNNER_TIMEOUT", "summary": "Runner job wait timed out", "error": "RUNNER_TIMEOUT", "stage": "RUNNER_WAIT"}

    async def cancel_job(self, job_id: str) -> dict:
        async with httpx.AsyncClient(timeout=10, trust_env=True) as client:
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
            async with httpx.AsyncClient(timeout=45, trust_env=True) as client:
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
        async with httpx.AsyncClient(timeout=15, trust_env=True) as client:
            response = await client.delete(
                f"{self.base_url}/api/v1/workspaces/{run_id}", headers=self._headers()
            )
            response.raise_for_status()

    async def clear_sessions(self, run_id: str) -> None:
        async with httpx.AsyncClient(timeout=10, trust_env=True) as client:
            response = await client.delete(f"{self.base_url}/api/v1/sessions/{run_id}", headers=self._headers())
            response.raise_for_status()


runner_client = RunnerClient()
