from __future__ import annotations

from .cli import CliWorkerEngine


class CodexCliEngine(CliWorkerEngine):
    """Headless Codex CLI adapter used by the standalone process driver.

    argv and parsing come from ``muteki.solver.cli_driver.CodexDriver``
    (``exec --json`` plus the vendored sandbox/approval handling) so a Codex
    Worker reports the same session id, token usage and structured-error
    classification as the container path.
    """

    engine = "codex"


__all__ = ["CodexCliEngine"]
