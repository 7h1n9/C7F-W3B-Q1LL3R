"""Durable, schema-compatible Muteki runtime selections.

Run rows predate multi-engine Muteki execution.  The selection therefore lives
in the existing ``hints_json`` column while the public API exposes a typed
contract.  This module is deliberately independent from the SQLAlchemy model
so the canonical Coordinator can consume the same selection in tests and in
production.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

SUPPORTED_WORKER_ENGINE_TYPES = frozenset({"codex_cli", "openai_compatible"})
MUTEKI_RUNTIME_HINT_KEY = "muteki_runtime"
_ENGINE_ALIASES = {
    "codex": "codex_cli",
    "codex-sdk": "codex_cli",
    "codex_sdk": "codex_cli",
    "codex-cli": "codex_cli",
    "codex-api": "codex_cli",
    "openai-compatible": "openai_compatible",
}


@dataclass(frozen=True, slots=True)
class WorkerEngineSelection:
    """One Worker engine selected for a Muteki run."""

    engine_type: str
    model_config_id: str | None = None

    def __post_init__(self) -> None:
        engine_type = _ENGINE_ALIASES.get(self.engine_type.strip().casefold(), self.engine_type.strip().casefold())
        if engine_type not in SUPPORTED_WORKER_ENGINE_TYPES:
            raise ValueError(f"unsupported worker engine type: {self.engine_type}")
        if engine_type == "openai_compatible" and not self.model_config_id:
            raise ValueError("openai_compatible worker requires model_config_id")
        if engine_type == "codex_cli" and not self.model_config_id:
            raise ValueError("codex_cli worker requires model_config_id")
        object.__setattr__(self, "engine_type", engine_type)
        if self.model_config_id:
            object.__setattr__(self, "model_config_id", str(self.model_config_id))

    @property
    def engine_id(self) -> str:
        """Return the stable Worker identity used by Coordinator scheduling."""

        if self.engine_type == "openai_compatible":
            return f"openai-compatible:{self.model_config_id}"
        return f"codex-cli:{self.model_config_id}"

    def to_dict(self) -> dict[str, str]:
        value = {"engine_type": self.engine_type, "engine_id": self.engine_id}
        if self.model_config_id:
            value["model_config_id"] = self.model_config_id
        return value


def normalize_worker_engines(
    selections: Iterable[Mapping[str, Any]] | None,
    *,
    fallback_engine_type: str | None = None,
    fallback_model_config_id: str | None = None,
) -> tuple[WorkerEngineSelection, ...]:
    """Normalize Worker selections, skipping removed/legacy engine types."""

    normalized: list[WorkerEngineSelection] = []
    for item in selections or ():
        if not isinstance(item, Mapping):
            continue
        engine_type = str(item.get("engine_type") or "").strip().casefold()
        if not engine_type:
            continue
        model_config_id = str(item["model_config_id"]) if item.get("model_config_id") else None
        resolved = _ENGINE_ALIASES.get(engine_type, engine_type)
        if resolved == "mock":
            # mock engine was removed; drop these legacy selections.
            continue
        if resolved == "codex_cli" and not model_config_id:
            # Legacy codex_sdk/codex-cli without a model config cannot run.
            continue
        try:
            normalized.append(WorkerEngineSelection(resolved, model_config_id))
        except ValueError:
            continue
    if normalized:
        return tuple(_dedupe(normalized))
    if not fallback_engine_type:
        return ()
    fallback = str(fallback_engine_type).strip().casefold()
    resolved = _ENGINE_ALIASES.get(fallback, fallback)
    if resolved == "mock" or resolved not in SUPPORTED_WORKER_ENGINE_TYPES:
        return ()
    if resolved == "codex_cli" and not fallback_model_config_id:
        return ()
    try:
        return (
            WorkerEngineSelection(
                resolved,
                str(fallback_model_config_id) if fallback_model_config_id else None,
            ),
        )
    except ValueError:
        return ()


def runtime_selection_from_hints(
    hints: Mapping[str, Any] | None,
    *,
    fallback_engine_type: str | None = None,
    fallback_model_config_id: str | None = None,
) -> tuple[str | None, tuple[WorkerEngineSelection, ...]]:
    """Read the additive Muteki selection from existing Run hints."""

    runtime = (hints or {}).get(MUTEKI_RUNTIME_HINT_KEY, {})
    if not isinstance(runtime, Mapping):
        runtime = {}
    worker_engines = normalize_worker_engines(
        runtime.get("worker_engines") if isinstance(runtime.get("worker_engines"), list) else None,
        fallback_engine_type=fallback_engine_type,
        fallback_model_config_id=fallback_model_config_id,
    )
    reason_model_config_id = runtime.get("reason_model_config_id")
    return (
        str(reason_model_config_id) if reason_model_config_id else None,
        worker_engines,
    )


def write_runtime_selection(
    hints: Mapping[str, Any] | None,
    *,
    reason_model_config_id: str | None,
    worker_engines: Iterable[WorkerEngineSelection],
) -> dict[str, Any]:
    """Merge the selection into existing hints without dropping challenge hints."""

    result = dict(hints or {})
    result[MUTEKI_RUNTIME_HINT_KEY] = {
        "reason_model_config_id": reason_model_config_id,
        "worker_engines": [item.to_dict() for item in worker_engines],
    }
    return result


def _dedupe(items: Iterable[WorkerEngineSelection]) -> list[WorkerEngineSelection]:
    seen: set[tuple[str, str | None]] = set()
    result: list[WorkerEngineSelection] = []
    for item in items:
        key = (item.engine_type, item.model_config_id)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


__all__ = [
    "MUTEKI_RUNTIME_HINT_KEY",
    "SUPPORTED_WORKER_ENGINE_TYPES",
    "WorkerEngineSelection",
    "normalize_worker_engines",
    "runtime_selection_from_hints",
    "write_runtime_selection",
]
