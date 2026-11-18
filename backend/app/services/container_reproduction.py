"""Run the reproduction commands inside the Muteki worker container.

Why this module exists
----------------------
Fresh reproduction used to dispatch through ``runner_client`` -- the legacy Kali
Runner, a separate VM-side HTTP service.  That service is not part of this
deployment: ``legacy_kali_runner`` defaults to False, so ``_require_enabled()``
raised ``RunnerRetiredError`` at the first call.  The result was that **no Run
ever produced a verified reproduction**:

    arena.fresh_reproduction events   -> 37, all reason="RunnerRetiredError"
    solve_runs.fresh_reproduction_verified=1  -> 0 out of 742

The verification code was not broken; it was wired to a retired transport.

The commands themselves were never Runner-specific.  ``ReproductionCommandRenderer``
already turns a step into an ordinary shell command (``curl ...``, ``sqlmap ...``),
and the Muteki worker image contains those tools.  So the fix is to execute the
rendered command in the container the solver already uses, rather than posting a
job to a service that no longer exists.

Design notes
------------
* **Synchronous.**  ``docker exec`` via ``subprocess`` blocks.  The caller is
  ``async`` and bridges with ``asyncio.to_thread``; keeping this layer sync avoids
  colouring the vendored container module with ``async``.
* **Reuse-or-create.**  The solver's container is torn down when the Coordinator
  finalises the Run, and reproduction runs *after* the arena accepts the flag --
  so the container may or may not still exist.  ``ensure_container`` is idempotent
  and handles both.
* **No shell interpolation of untrusted text.**  The command is passed as a single
  ``sh -c`` argument to ``docker exec``; it was produced by the renderer, which
  quotes each argv element with ``shlex.join``.  It is not re-quoted here, because
  re-quoting a shell command string would break it.
"""

from __future__ import annotations

import asyncio
import re
import shlex
from pathlib import Path
from typing import Any

from muteki.solver.container_exec import (
    CONTAINER_WORKSPACE,
    _docker,
    _run_container_name,
    ensure_container,
)

#: Wall-clock ceiling for one rendered command.  The whole replay is separately
#: bounded by ``REPRODUCTION_BUDGET_SECONDS`` in ``arena_solve``; this only stops a
#: single hung command from eating that entire budget.
DEFAULT_COMMAND_TIMEOUT_SECONDS = 120

#: How much of a command's stdout/stderr is kept.  A full ``sqlmap --dump`` can be
#: megabytes and it all goes into a JSON validation file; the flag pattern only
#: ever appears in the head or the tail, so keep both.
MAX_CAPTURE_CHARS = 20000


def container_name_for(run_id: str) -> str:
    """The container this Run's solver used, whether or not it still exists."""

    return _run_container_name(run_id)


def _truncate(text: str) -> str:
    if len(text) <= MAX_CAPTURE_CHARS:
        return text
    half = MAX_CAPTURE_CHARS // 2
    return f"{text[:half]}\n...<{len(text) - MAX_CAPTURE_CHARS} chars elided>...\n{text[-half:]}"


def _missing_module(stderr: str) -> str | None:
    """The module a failing PoC could not import, if that is why it failed.

    Worth separating from a plain non-zero exit.  ``ModuleNotFoundError`` means the
    replay never exercised the exploit -- the Worker's script assumed a library the
    worker image does not ship.  That is an image problem with a known fix, not
    evidence that the chain does not reproduce, and the two must not look alike in
    the validation file.

    Measured 2026-11-10 on ``[De1CTF 2019]Giftbox`` (run ``9847adf6``):
    ``ModuleNotFoundError: No module named 'pyotp'`` -- the TOTP exploit could not
    even start, yet the *only* visible symptom was ``status: FAILED, exit: 1``.
    """

    match = re.search(r"ModuleNotFoundError: No module named '([^']+)'", stderr or "")
    return match.group(1) if match else None


class ContainerCommandRunner:
    """Execute rendered reproduction commands in the Run's worker container."""

    def __init__(self, *, timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS) -> None:
        self.timeout_seconds = timeout_seconds

    def _handle(self, run_id: str, workspace: Path):
        """Reuse the Run's container, or bring one up.

        ``ensure_container`` is idempotent: it ``docker start``s an existing one,
        removes a dead one, and creates a fresh one otherwise.  Reproduction runs
        after the solver finished, so "already gone" is the common case and this
        is a create, not an error.
        """

        return ensure_container(run_id, str(workspace))

    def workspace_path(self, run_id: str, workspace: Path) -> str:
        """The container-side path of the Run's workspace (``/home/kali/workspace``).

        Needed by callers that build a command *string* (a PoC re-run) rather than
        an argv list, because ``sh -c`` runs inside the container and must not see
        a host path.  Falls back to the fixed mount point when the handle cannot be
        resolved -- the mount point is a constant of the image.
        """

        try:
            return self._handle(run_id, workspace).to_container_cwd(str(workspace))
        except Exception:  # noqa: BLE001 - a display path must never fail a replay
            return CONTAINER_WORKSPACE

    def run(self, run_id: str, workspace: Path, command: str) -> dict[str, Any]:
        """Run one command.  Never raises: a failure is a result, not an exception.

        Reproduction is diagnostic -- the solve has already been accepted.  An
        exception here would be caught by ``_verify_reproduction`` and recorded as
        an opaque ``error``, which is exactly the state this module exists to end.
        Every outcome must be data the validation file can show.
        """

        handle = self._handle(run_id, workspace)
        container = handle.container
        cwd = handle.to_container_cwd(str(workspace))
        try:
            completed = _docker(
                "exec",
                "-w", cwd,
                container,
                "sh", "-c", command,
                timeout=self.timeout_seconds,
            )
        except Exception as error:  # noqa: BLE001 - a failed replay must stay data
            return {
                "status": "FAILED",
                "error_code": "CONTAINER_EXEC_RAISED",
                "summary": f"{type(error).__name__}: {error}",
                "body": "",
                "exit_code": None,
                "container": container,
                "cwd": cwd,
            }

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        # ``status`` mirrors what the Runner used to report so the rest of
        # ``execute`` (which checks ``status != "COMPLETED"``) keeps working.
        # A non-zero exit is a FAILED *command*, not a failed *reproduction* --
        # ``curl`` exits non-zero on plenty of responses that still carry the
        # flag, so the flag scan below runs on the body regardless.
        status = "COMPLETED" if completed.returncode == 0 else "FAILED"
        missing = _missing_module(stderr)
        return {
            "status": status,
            "error_code": (
                None if completed.returncode == 0
                else "MISSING_DEPENDENCY" if missing
                else "NONZERO_EXIT"
            ),
            "missing_module": missing,
            "summary": (
                f"PoC needs '{missing}', which the worker image does not ship"
                if missing
                else _truncate(stderr.strip())[:500]
            ),
            "body": _truncate(stdout),
            "stderr": _truncate(stderr),
            "exit_code": completed.returncode,
            "container": container,
            "cwd": cwd,
            "command": command,
        }

    async def run_async(self, run_id: str, workspace: Path, command: str) -> dict[str, Any]:
        """``run`` off the event loop."""

        return await asyncio.to_thread(self.run, run_id, workspace, command)


def build_curl_body(command: str) -> str:
    """Kept for symmetry with the old Runner result shape.

    The Runner returned a parsed ``body`` field; ``execute`` reads it to detect
    ``{"matched": true}``.  Executing in a container gives us raw stdout, so the
    body is whatever ``curl -i`` printed -- headers included.  Callers that need
    the JSON payload should parse it themselves.
    """

    return command


container_command_runner = ContainerCommandRunner()
