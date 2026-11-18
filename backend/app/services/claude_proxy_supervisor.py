"""Supervise the standalone Claude proxy from the backend process.

Why a supervisor instead of a one-shot spawn
--------------------------------------------
The proxy is a separate process because it must bind ``0.0.0.0`` on a random
port, and because the Worker containers reach it by host alias -- neither of
which the backend's own socket provides. It is started here so an operator does
not have to keep a second terminal open, and supervised so the two stay in sync:

* **Started with the backend**, after the state file path is known.
* **Stopped with the backend**, including on a crash -- the child carries a
  ``--parent-pid`` watchdog so it cannot outlive us and keep spending model
  credentials from a port nobody is watching.
* **Restarted once if it dies early**, because a proxy that failed to bind (a
  port taken between the probe and the bind) is worth one retry, while a crash
  loop is not.

What this deliberately does not do
----------------------------------
It does not read the port back and cache it. The state file is the single source
of truth, and :func:`app.core.config.claude_proxy_endpoint` re-reads it (with a
liveness probe) on each use. Caching here would create a second copy of the
truth that can go stale when the child restarts on a new port.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from app.core.config import get_settings

logger = logging.getLogger(__name__)

#: How long to wait for the child to publish its state file before giving up.
#: Generous relative to the work involved (import + bind) because the first
#: import of uvicorn and the app on a cold cache is the slow part.
_STARTUP_TIMEOUT_SECONDS = 25.0

#: Poll interval while waiting for the state file to appear and go live.
_POLL_INTERVAL_SECONDS = 0.25


class ClaudeProxySupervisor:
    """Owns the proxy child process for the lifetime of the backend."""

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        """Start the proxy unless it is already up.

        An existing proxy is left alone rather than replaced: it may be one the
        operator started by hand (which is the documented way to run it without
        auto-start), and killing it to start an identical one would drop its
        token out from under any Worker already using it.
        """

        settings = get_settings()
        if not settings.claude_proxy_autostart:
            logger.info("Claude proxy autostart is disabled; not starting it.")
            return

        from app.core.config import claude_proxy_endpoint

        if claude_proxy_endpoint() is not None:
            logger.info("Claude proxy is already listening; leaving it as is.")
            return

        async with self._lock:
            if self.running:
                return
            # Re-check under the lock: two callers could both have passed the
            # unlocked check above before either took the lock.
            if claude_proxy_endpoint() is not None:
                return
            await self._spawn(settings.claude_proxy_state_file)

    async def _spawn(self, state_file: str) -> None:
        backend_root = Path(__file__).resolve().parents[2]
        state_path = Path(state_file)
        # A leftover file from a killed run would make the wait below succeed
        # against a dead port, so it is cleared first -- the child rewrites it.
        state_path.unlink(missing_ok=True)

        command = [
            sys.executable,
            "-m",
            "app.claude_proxy_launcher",
            "--state-file",
            str(state_path),
            "--parent-pid",
            str(os.getpid()),
        ]
        logger.info("Starting Claude proxy: %s", " ".join(command))
        try:
            self._process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(backend_root),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as error:
            # Not fatal: a Claude channel simply falls back to its stored
            # base_url, which is the pre-proxy behaviour.
            logger.warning("Could not start the Claude proxy: %s", error)
            return

        if await self._await_listening(state_path):
            logger.info("Claude proxy is up (pid %s).", self._process.pid)
        else:
            logger.warning(
                "Claude proxy did not publish a listening port within %.0fs.",
                _STARTUP_TIMEOUT_SECONDS,
            )

    async def _await_listening(self, state_path: Path) -> bool:
        """Wait until the child's state file exists and its port answers."""

        from app.core.config import claude_proxy_endpoint

        deadline = asyncio.get_running_loop().time() + _STARTUP_TIMEOUT_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            if self._process is not None and self._process.returncode is not None:
                return False
            if state_path.exists() and claude_proxy_endpoint() is not None:
                return True
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        return False

    async def stop(self) -> None:
        """Terminate the child and wait for it to go, then clear its state file.

        ``terminate`` first: the launcher removes its state file in a ``finally``,
        and a SIGKILL would skip that and leave a stale file behind. The kill is
        only the fallback for a child that ignores the polite request.

        The state file is unlinked unconditionally afterwards rather than being
        left to the child. The child only cleans up when it gets to run its
        ``finally`` -- which is exactly what does not happen when the whole
        process tree is killed at once, the case this method exists to handle.
        Removing a file the child already removed is a no-op, so doing it here
        costs nothing and covers the case where it could not.
        """

        process = self._process
        self._process = None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:
                logger.warning("Claude proxy ignored terminate; killing it.")
                process.kill()
                await process.wait()
        self._unlink_state_file()

    @staticmethod
    def _unlink_state_file() -> None:
        """Remove the proxy's state file if it is still there."""

        raw = str(get_settings().claude_proxy_state_file or "").strip()
        if not raw:
            return
        try:
            Path(raw).unlink(missing_ok=True)
        except OSError as error:
            logger.debug("Could not remove the Claude proxy state file: %s", error)


claude_proxy_supervisor = ClaudeProxySupervisor()
