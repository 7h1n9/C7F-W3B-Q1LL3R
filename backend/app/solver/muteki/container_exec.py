"""Optional Docker execution backend for standalone Muteki workers.

This module uses the Docker CLI instead of importing the optional Docker SDK.
That keeps the host application importable when Docker is not installed while
still making the exact mount and environment contract testable.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence


@dataclass(frozen=True, slots=True)
class ContainerResult:
    success: bool
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    command: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class SkillResult:
    success: bool
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    command: tuple[str, ...] = field(default_factory=tuple)


class ContainerExecutor:
    """Run one CLI Driver worker with only workspace/blackboard bind mounts."""

    def __init__(self, image: str | None = None, *, docker_binary: str = "docker", timeout_seconds: int = 300, network: str | None = None, environment: Mapping[str, str] | None = None) -> None:
        self.image = image or os.environ.get("MUTEKI_WORKER_IMAGE", "muteki-worker:latest")
        self.docker_binary = docker_binary
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.network = network or os.environ.get("MUTEKI_CONTAINER_NETWORK", "bridge")
        self.environment = {str(key): str(value) for key, value in (environment or {}).items() if _allowed_env(str(key))}

    def available(self) -> bool:
        return shutil.which(self.docker_binary) is not None

    def build_worker_command(
        self,
        *,
        intent_id: str,
        workspace_path: str,
        blackboard_path: str,
        engine: str = "codex",
        worker_id: str | None = None,
        challenge_id: str | None = None,
    ) -> list[str]:
        workspace = _existing_directory(workspace_path, "workspace")
        blackboard = _existing_file(blackboard_path, "blackboard")
        safe_name = _safe_name(f"muteki-{worker_id or intent_id}")
        command = [
            self.docker_binary,
            "run",
            "--rm",
            "--name",
            safe_name,
            "--mount",
            f"type=bind,source={workspace},target=/workspace",
            "--mount",
            f"type=bind,source={blackboard},target=/muteki/shared_graph.db",
            "--env",
            "PYTHONPATH=/app/backend",
            "--env",
            "MUTEKI_BLACKBOARD_DB=/muteki/shared_graph.db",
            "--env",
            "MUTEKI_WORKSPACE=/workspace",
            "--env",
            f"MUTEKI_WORKER_ID={worker_id or intent_id}",
            "--env",
            f"MUTEKI_CHALLENGE_ID={challenge_id or 'container-run'}",
        ]
        for key, value in sorted(self.environment.items()):
            command.extend(["--env", f"{key}={value}"])
        if self.network:
            command.extend(["--network", self.network])
        command.extend([
            self.image,
            "--mode",
            "worker",
            "--engine",
            engine,
            "--intent-id",
            intent_id,
            "--workspace",
            "/workspace",
            "--blackboard",
            "/muteki/shared_graph.db",
        ])
        return command

    def run_worker(
        self,
        intent_id: str,
        workspace_path: str,
        blackboard_path: str,
        engine: str = "codex",
        timeout: int | None = None,
        *,
        worker_id: str | None = None,
        challenge_id: str | None = None,
    ) -> ContainerResult:
        command = self.build_worker_command(intent_id=intent_id, workspace_path=workspace_path, blackboard_path=blackboard_path, engine=engine, worker_id=worker_id, challenge_id=challenge_id)
        return self._run(command, timeout=timeout)

    async def run_worker_async(self, *args, **kwargs) -> ContainerResult:
        return await asyncio.to_thread(self.run_worker, *args, **kwargs)

    def build_skill_command(self, *, skill_cmd: str, skill_args: Sequence[str], blackboard_path: str, worker_id: str | None = None, challenge_id: str | None = None) -> list[str]:
        blackboard = _existing_file(blackboard_path, "blackboard")
        safe_name = _safe_name(f"muteki-skill-{worker_id or 'worker'}")
        command = [
            self.docker_binary,
            "run",
            "--rm",
            "--name",
            safe_name,
            "--mount",
            f"type=bind,source={blackboard},target=/muteki/shared_graph.db",
            "--env",
            "PYTHONPATH=/app/backend",
            "--env",
            "MUTEKI_BLACKBOARD_DB=/muteki/shared_graph.db",
            "--env",
            f"MUTEKI_WORKER_ID={worker_id or 'skill-worker'}",
            "--env",
            f"MUTEKI_CHALLENGE_ID={challenge_id or 'skill-run'}",
        ]
        for key, value in sorted(self.environment.items()):
            command.extend(["--env", f"{key}={value}"])
        if self.network:
            command.extend(["--network", self.network])
        command.extend([self.image, "--mode", "skill", "--skill-cmd", skill_cmd, "--skill-args", *[str(item) for item in skill_args]])
        return command

    def run_skill(self, skill_cmd: str, skill_args: list[str], blackboard_path: str, *, worker_id: str | None = None, challenge_id: str | None = None, timeout: int | None = None) -> SkillResult:
        command = self.build_skill_command(skill_cmd=skill_cmd, skill_args=skill_args, blackboard_path=blackboard_path, worker_id=worker_id, challenge_id=challenge_id)
        result = self._run(command, timeout=timeout)
        return SkillResult(result.success, result.returncode, result.stdout, result.stderr, result.timed_out, result.command)

    async def run_skill_async(self, *args, **kwargs) -> SkillResult:
        return await asyncio.to_thread(self.run_skill, *args, **kwargs)

    def _run(self, command: list[str], *, timeout: int | None) -> ContainerResult:
        if not self.available():
            return ContainerResult(False, -1, stderr="DOCKER_NOT_AVAILABLE", command=tuple(command))
        try:
            completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=max(1, int(timeout or self.timeout_seconds)), check=False)
        except subprocess.TimeoutExpired as error:
            _stop_container(command, self.docker_binary)
            return ContainerResult(False, -1, stdout=_bounded(error.stdout), stderr="CONTAINER_TIMEOUT", timed_out=True, command=tuple(command))
        except OSError as error:
            return ContainerResult(False, -1, stderr=f"DOCKER_EXEC_ERROR:{error}", command=tuple(command))
        return ContainerResult(completed.returncode == 0, int(completed.returncode), _bounded(completed.stdout), _bounded(completed.stderr), command=tuple(command))


def _existing_directory(value: str, label: str) -> str:
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"{label} directory does not exist: {path}")
    return str(path)


def _existing_file(value: str, label: str) -> str:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"{label} file does not exist: {path}")
    return str(path)


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", value)[:120]


def _allowed_env(key: str) -> bool:
    return key.startswith(("MUTEKI_", "ANTHROPIC_", "CLAUDE_", "CODEX_", "CURSOR_", "OPENAI_"))


def _bounded(value: str | bytes | None, limit: int = 12000) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return str(value or "")[-limit:]


def _stop_container(command: Sequence[str], docker_binary: str) -> None:
    try:
        name_index = list(command).index("--name") + 1
        name = list(command)[name_index]
    except (ValueError, IndexError):
        return
    subprocess.run([docker_binary, "rm", "-f", name], capture_output=True, check=False)


__all__ = ["ContainerExecutor", "ContainerResult", "SkillResult"]
