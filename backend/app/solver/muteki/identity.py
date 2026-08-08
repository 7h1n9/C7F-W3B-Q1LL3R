"""Engine identity and capability detection for standalone Muteki workers."""

from __future__ import annotations

import os
import shutil
import subprocess
from enum import Enum
from typing import Any


class EngineType(str, Enum):
    CODEX = "codex"
    CLAUDE = "claude"
    CURSOR = "cursor"
    UNKNOWN = "unknown"


_BINARIES = {
    EngineType.CODEX: ("codex", "MUTEKI_CODEX_BIN"),
    EngineType.CLAUDE: ("claude", "MUTEKI_CLAUDE_BIN"),
    EngineType.CURSOR: ("cursor-agent", "MUTEKI_CURSOR_BIN"),
}


class IdentityModel:
    """Detect available engine CLIs without starting a model turn."""

    @staticmethod
    def detect_engine(preferred: str | EngineType | None = None) -> EngineType:
        candidates = _ordered_engines(preferred)
        for engine in candidates:
            if IdentityModel.binary_path(engine):
                return engine
        return EngineType.UNKNOWN

    @staticmethod
    def binary_path(engine: str | EngineType) -> str | None:
        normalized = _coerce_engine(engine)
        if normalized is None:
            return None
        binary, override = _BINARIES[normalized]
        configured = os.environ.get(override, "").strip()
        if configured:
            return configured if os.path.isfile(configured) or shutil.which(configured) else None
        return shutil.which(binary)

    @staticmethod
    def get_engine_version(engine: str | EngineType) -> str | None:
        normalized = _coerce_engine(engine)
        binary = IdentityModel.binary_path(engine)
        if normalized is None or binary is None:
            return None
        try:
            result = subprocess.run(
                [binary, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        output = (result.stdout or result.stderr or "").strip().splitlines()
        return output[-1][:300] if result.returncode == 0 and output else None

    @staticmethod
    def get_capabilities(engine: str | EngineType) -> dict[str, Any]:
        normalized = _coerce_engine(engine) or EngineType.UNKNOWN
        return {
            "engine": normalized.value,
            "supports_mcp": normalized is EngineType.CODEX,
            "supports_cli": normalized in {EngineType.CLAUDE, EngineType.CURSOR, EngineType.CODEX},
            "supports_tools": normalized in {EngineType.CODEX, EngineType.CURSOR},
            "max_context": {
                EngineType.CODEX: 128000,
                EngineType.CLAUDE: 200000,
                EngineType.CURSOR: 128000,
            }.get(normalized, 0),
        }

    @staticmethod
    def describe(engine: str | EngineType | None = None) -> dict[str, Any]:
        selected = IdentityModel.detect_engine(engine)
        return {
            "engine": selected.value,
            "version": IdentityModel.get_engine_version(selected),
            "binary": IdentityModel.binary_path(selected),
            "capabilities": IdentityModel.get_capabilities(selected),
        }


def _coerce_engine(value: str | EngineType | None) -> EngineType | None:
    if isinstance(value, EngineType):
        return None if value is EngineType.UNKNOWN else value
    try:
        return EngineType(str(value).casefold()) if value else None
    except ValueError:
        return None


def _ordered_engines(preferred: str | EngineType | None) -> tuple[EngineType, ...]:
    selected = _coerce_engine(preferred)
    values = [selected] if selected is not None else []
    values.extend(item for item in (EngineType.CODEX, EngineType.CLAUDE, EngineType.CURSOR) if item not in values)
    return tuple(values)


__all__ = ["EngineType", "IdentityModel"]
