"""Built-in heterogeneous WorkerEngine implementations."""

from .claude import ClaudeEngine
from .cli import CliWorkerEngine
from .codex import CodexCliEngine
from .cursor import CursorEngine

# `CodexEngine` used to be advertised here without ever being imported, so
# `from .engines import CodexEngine` raised ImportError while the name still
# appeared to exist. The class is `CodexCliEngine`.
ENGINE_CLASSES = {
    "claude": ClaudeEngine,
    "codex": CodexCliEngine,
    "cursor": CursorEngine,
}

__all__ = [
    "ENGINE_CLASSES",
    "ClaudeEngine",
    "CliWorkerEngine",
    "CodexCliEngine",
    "CursorEngine",
]
