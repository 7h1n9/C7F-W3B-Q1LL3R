from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any, Sequence

from ...graph import Intent
from ..engine import WorkerEngine, WorkerResult, intent_prompt

_METADATA_STDERR_LIMIT = 2000


class CliWorkerEngine(WorkerEngine):
    """Subprocess adapter that drives the vendored Muteki CLI drivers.

    argv construction, vendor JSON/JSONL parsing and usage accounting all come
    from ``muteki.solver.cli_driver`` instead of being re-derived here, so this
    engine cannot drift from the contract the containerised Worker runs on: the
    same permission flags, MCP/Web isolation, session handling and token/cost
    buckets apply on both paths.  Before this, Claude and Cursor were invoked
    with a hand-written argv that discarded the vendor's entire JSON envelope.
    """

    # Base engine name as understood by muteki.solver.cli_driver's registry.
    engine: str = ""

    def __init__(
        self,
        *,
        executable: str | None = None,
        timeout_seconds: int = 720,
        environment: dict[str, str] | None = None,
        web_access: bool = True,
        kb_access: bool = True,
    ) -> None:
        self._override = str(executable).strip() if executable else ""
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.environment = dict(environment or {})
        # Forwarded to the driver, which turns them into the engine's own
        # isolation flags (`--disallowed-tools WebSearch WebFetch`, and for
        # Claude `--mcp-config '{"mcpServers":{}}' --strict-mcp-config`). Defaults
        # mirror the driver's own defaults: both surfaces available.
        self.web_access = bool(web_access)
        self.kb_access = bool(kb_access)
        self._driver: Any = None

    @property
    def driver(self):
        """The vendored driver for this engine, resolved once on first use."""

        if self._driver is None:
            from muteki.solver.cli_driver import driver_for

            self._driver = driver_for(self.engine)
        return self._driver

    @property
    def executable(self) -> str:
        """The binary this engine will shell.

        An explicit override wins; otherwise the vendored resolver picks a
        runnable official install (see ``resolve_engine_bin``) rather than
        trusting PATH order -- which is also what a live Worker uses.
        """

        return self._override or str(self.driver.bin or "")

    def engine_type(self) -> str:
        return self.engine

    def build_argv(self, prompt: str, *, stream: bool = False) -> list[str]:
        """The exact argv this engine would run for ``prompt``."""

        argv = [
            str(part)
            for part in self.driver.build_execute(
                prompt,
                None,
                web_access=self.web_access,
                kb_access=self.kb_access,
                stream=stream,
            )
        ]
        if self._override and argv:
            argv[0] = self._override
        return argv

    def health_check(self) -> bool:
        """Cheap binary-presence check.

        Deliberately NOT the driver's ``health_detail``: that sends a real
        one-turn hello with retries, and this runs synchronously inside
        WorkerPool's selection loop where a 120s probe would stall every caller.
        """

        binary = self.executable
        if not binary:
            return False
        return Path(binary).is_file() or shutil.which(binary) is not None

    def _command(self, prompt: str) -> list[str]:
        """Deprecated alias for :meth:`build_argv`; kept for existing callers."""

        return self.build_argv(prompt)

    async def execute(self, intent: Intent, workspace: str) -> WorkerResult:
        root = Path(workspace).resolve()
        if not root.exists() or not root.is_dir():
            return WorkerResult(
                False, self.engine_type(), metadata={"reason": "WORKSPACE_NOT_FOUND"}
            )
        env = os.environ.copy()
        env.update(self.environment)
        try:
            completed = await asyncio.to_thread(
                _run_command,
                self.build_argv(intent_prompt(intent)),
                root,
                env,
                self.timeout_seconds,
            )
        except asyncio.TimeoutError as error:
            # Must precede OSError: TimeoutError is a subclass of it.
            return WorkerResult(
                False,
                self.engine_type(),
                metadata={
                    "reason": "WORKER_TIMEOUT",
                    "timeout_seconds": self.timeout_seconds,
                    "error": str(error)[:500],
                },
            )
        except OSError as error:
            return WorkerResult(
                False,
                self.engine_type(),
                metadata={"reason": type(error).__name__, "error": str(error)[:500]},
            )
        from muteki.solver.cli_driver import finalize_cli_result

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        result = finalize_cli_result(
            self.driver.parse(stdout, stderr),
            driver_name=self.driver.name,
            stdout=stdout,
            stderr=stderr,
            returncode=completed.returncode,
        )
        return WorkerResult(
            completed.returncode == 0 and not result.error,
            self.engine_type(),
            # The parsed assistant transcript, not the raw stdout blob: this is
            # what lands on the Blackboard as an unverified WORKER_RESULT fact,
            # so it has to be the agent's answer rather than a JSON envelope.
            output=result.text,
            metadata=_metadata(self.driver.name, completed.returncode, result, stderr),
        )


def _metadata(driver_name: str, returncode: int, result: Any, stderr: str) -> dict[str, Any]:
    """Bounded, JSON-serializable diagnostics for one CLI turn."""

    metadata: dict[str, Any] = {
        "driver": driver_name,
        "returncode": returncode,
        "session": result.session,
        "num_turns": result.num_turns,
        "elapsed_s": round(float(result.elapsed_s or 0.0), 3),
        "cost_usd": result.cost_usd,
        # Kept beside the number it qualifies: an estimated figure must never be
        # read as a metered one by the cost ledger or the deck.
        "cost_estimated": bool(result.cost_estimated),
        "usage": {
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cache_read_tokens": result.cache_read_tokens,
            "cache_write_tokens": result.cache_write_tokens,
            "reasoning_tokens": result.reasoning_tokens,
            "estimated": bool(result.usage_estimated),
            "usage_id": result.usage_id,
        },
        "stderr": stderr[-_METADATA_STDERR_LIMIT:],
    }
    for flag, reason in (
        ("timed_out", "WORKER_TIMEOUT"),
        ("oom_killed", "WORKER_OOM_KILLED"),
        ("output_limit", "WORKER_OUTPUT_LIMIT"),
        ("disk_limit", "WORKER_DISK_LIMIT"),
        ("cancelled", "WORKER_CANCELLED"),
    ):
        if getattr(result, flag, False):
            metadata[flag] = True
            metadata["reason"] = reason
    if result.error:
        # Vendor/protocol failure detail stays in metadata; it must never be
        # promoted into ``output``, where the provenance gate could read it as
        # an agent reply.
        metadata.setdefault("reason", "ENGINE_FAILED")
        metadata["error"] = str(result.error)[:500]
    return metadata


def _run_command(command: Sequence[str], cwd: Path, env: dict[str, str], timeout_seconds: int):
    import subprocess

    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as error:
        raise asyncio.TimeoutError(str(error)) from error


__all__ = ["CliWorkerEngine"]
