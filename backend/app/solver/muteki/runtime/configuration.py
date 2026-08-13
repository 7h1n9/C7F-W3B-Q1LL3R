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

SUPPORTED_WORKER_ENGINE_TYPES = frozenset({"mock", "codex_sdk", "openai_compatible"})
MUTEKI_RUNTIME_HINT_KEY = "muteki_runtime"
_ENGINE_ALIASES = {
    "codex": "codex_sdk",
    "codex-sdk": "codex_sdk",
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
        if engine_type != "openai_compatible" and self.model_config_id:
            raise ValueError(f"{engine_type} worker does not accept model_config_id")
        object.__setattr__(self, "engine_type", engine_type)
        if self.model_config_id:
            object.__setattr__(self, "model_config_id", str(self.model_config_id))

    @property
    def engine_id(self) -> str:
        """Return the stable Worker identity used by Coordinator scheduling."""

        if self.engine_type == "codex_sdk":
            # The vendored official driver calls this engine ``codex``.
            return "codex"
        if self.engine_type == "openai_compatible":
            return f"openai-compatible:{self.model_config_id}"
        return "mock"

    def to_dict(self) -> dict[str, str]:
        value = {"engine_type": self.engine_type, "engine_id": self.engine_id}
        if self.model_config_id:
            value["model_config_id"] = self.model_config_id
        return value


def normalize_worker_engines(
    selections: Iterable[Mapping[str, Any]] | None,
    *,
    fallback_engine_type: str = "mock",
    fallback_model_config_id: str | None = None,
) -> tuple[WorkerEngineSelection, ...]:
    """Normalize new and legacy Run selections without silently downgrading."""

    normalized: list[WorkerEngineSelection] = []
    for item in selections or ():
        if not isinstance(item, Mapping):
            continue
        engine_type = str(item.get("engine_type") or "").strip().casefold()
        if not engine_type:
            continue
        normalized.append(
            WorkerEngineSelection(
                engine_type,
                str(item["model_config_id"]) if item.get("model_config_id") else None,
            )
        )
    if normalized:
        return tuple(_dedupe(normalized))
    return (
        WorkerEngineSelection(
            str(fallback_engine_type or "mock").strip().casefold(),
            str(fallback_model_config_id) if fallback_model_config_id else None,
        ),
    )


def runtime_selection_from_hints(
    hints: Mapping[str, Any] | None,
    *,
    fallback_engine_type: str = "mock",
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
