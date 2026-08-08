from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Sequence

from ...graph import Intent
from ..engine import WorkerEngine, WorkerResult, intent_prompt


class CliWorkerEngine(WorkerEngine):
    """Safe subprocess adapter shared by Claude Code and cursor-agent."""

    binary: str = ""

    def __init__(self, *, executable: str | None = None, timeout_seconds: int = 720, environment: dict[str, str] | None = None) -> None:
        self.executable = executable or self.binary
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.environment = dict(environment or {})

    def health_check(self) -> bool:
        return bool(shutil.which(self.executable))

    def _command(self, prompt: str) -> list[str]:
        raise NotImplementedError

    async def execute(self, intent: Intent, workspace: str) -> WorkerResult:
        root = Path(workspace).resolve()
        if not root.exists() or not root.is_dir():
            return WorkerResult(False, self.engine_type(), metadata={"reason": "WORKSPACE_NOT_FOUND"})
        env = os.environ.copy()
        env.update(self.environment)
        try:
            completed = await asyncio.to_thread(
                _run_command,
                self._command(intent_prompt(intent)),
                root,
                env,
                self.timeout_seconds,
            )
        except (OSError, asyncio.TimeoutError) as error:
            return WorkerResult(False, self.engine_type(), metadata={"reason": type(error).__name__, "error": str(error)[:500]})
        output = (completed.stdout or "")[-12000:]
        stderr = (completed.stderr or "")[-2000:]
        return WorkerResult(
            completed.returncode == 0,
            self.engine_type(),
            output=output,
            metadata={"returncode": completed.returncode, "stderr": stderr},
        )


def _run_command(command: Sequence[str], cwd: Path, env: dict[str, str], timeout_seconds: int):
    import subprocess

    try:
        return subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout_seconds, check=False)
    except subprocess.TimeoutExpired as error:
        raise asyncio.TimeoutError(str(error)) from error


__all__ = ["CliWorkerEngine"]
