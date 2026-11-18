"""Configuration helpers for first-version knowledge augmentation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, cast

KnowledgeMode = Literal["off", "on"]

_VALID_MODES = frozenset({"off", "on"})
DEFAULT_KNOWLEDGE_ROOT = Path(__file__).resolve().parents[3] / "data" / "knowledge"
# Distilled trajectory templates live inside the knowledge root but in their own
# subdirectory: the JSONL corpus loader globs ``*.jsonl`` directly under the
# root and the template store globs ``*.yaml`` under this one, so the two
# knowledge channels can never shadow each other.
DEFAULT_KNOWLEDGE_TEMPLATES_DIRNAME = "templates"


@dataclass(frozen=True)
class KnowledgeConfig:
    """Resolved runtime configuration for knowledge retrieval."""

    mode: KnowledgeMode
    root: Path
    top_k: int
    # When set, entries without a verifiable ``published_at`` are excluded from
    # retrieval.  The generalisation experiment needs a per-entry publication
    # date to build a "knowledge that existed before this challenge" snapshot;
    # a placeholder date makes that split silently meaningless, so an operator
    # running the time-held-out experiment must be able to exclude them.
    require_published_at: bool = False

    def __post_init__(self) -> None:
        mode = _normalize_mode(self.mode, source="KnowledgeConfig.mode")

        if not isinstance(self.require_published_at, bool):
            raise ValueError(
                "KnowledgeConfig.require_published_at must be a boolean; "
                f"got {self.require_published_at!r}"
            )

        if isinstance(self.top_k, bool) or not isinstance(self.top_k, int):
            raise ValueError(
                f"KnowledgeConfig.top_k must be an integer >= 1; got {self.top_k!r}"
            )
        if self.top_k < 1:
            raise ValueError(
                f"KnowledgeConfig.top_k must be an integer >= 1; got {self.top_k!r}"
            )

        if isinstance(self.root, str) and not self.root.strip():
            raise ValueError("KnowledgeConfig.root must not be empty")
        try:
            root = Path(self.root).expanduser()
        except TypeError as exc:
            raise ValueError(f"KnowledgeConfig.root must be path-like; got {self.root!r}") from exc

        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "root", root)


def load_knowledge_config() -> KnowledgeConfig:
    """Load and validate knowledge configuration from environment variables."""

    raw_mode = os.getenv("C7F_KNOWLEDGE_MODE", "off")
    mode = _normalize_mode(raw_mode, source="C7F_KNOWLEDGE_MODE")

    raw_root = os.getenv("C7F_KNOWLEDGE_ROOT")
    if raw_root is None:
        root = DEFAULT_KNOWLEDGE_ROOT
    else:
        if not raw_root.strip():
            raise ValueError("C7F_KNOWLEDGE_ROOT must not be empty")
        root = Path(raw_root).expanduser()

    raw_top_k = os.getenv("C7F_KNOWLEDGE_TOP_K", "3")
    try:
        top_k = int(raw_top_k.strip())
    except (AttributeError, ValueError) as exc:
        raise ValueError(
            f"C7F_KNOWLEDGE_TOP_K must be an integer >= 1; got {raw_top_k!r}"
        ) from exc
    if top_k < 1:
        raise ValueError(
            f"C7F_KNOWLEDGE_TOP_K must be an integer >= 1; got {raw_top_k!r}"
        )

    return KnowledgeConfig(
        mode=mode,
        root=root,
        top_k=top_k,
        require_published_at=_env_flag("C7F_KNOWLEDGE_REQUIRE_PUBLISHED_AT", False),
    )


def _env_flag(name: str, default: bool) -> bool:
    """Read a boolean environment flag, rejecting anything ambiguous."""

    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"{name} must be a boolean flag; got {raw!r}")


def knowledge_templates_root(config: KnowledgeConfig | None = None) -> Path:
    """Directory that distilled trajectory templates are stored in.

    Resolved from ``C7F_KNOWLEDGE_TEMPLATES_ROOT`` when set, otherwise from the
    active knowledge root.  This is the single source of truth shared by the
    writer (post-run distillation) and the reader (Worker/Reason recall), so the
    two ends of the loop can never drift apart again.
    """

    override = os.getenv("C7F_KNOWLEDGE_TEMPLATES_ROOT")
    if override is not None:
        if not override.strip():
            raise ValueError("C7F_KNOWLEDGE_TEMPLATES_ROOT must not be empty")
        return Path(override).expanduser()

    resolved = config if config is not None else load_knowledge_config()
    return Path(resolved.root).expanduser() / DEFAULT_KNOWLEDGE_TEMPLATES_DIRNAME


def resolve_knowledge_mode(
    run_hints: Mapping[str, object] | None,
    default: str | None = None,
) -> KnowledgeMode | None:
    """Resolve mode from top-level run hints, falling back to an optional default."""

    if run_hints is not None and "knowledge_mode" in run_hints:
        return _normalize_mode(
            run_hints["knowledge_mode"],
            source="run_hints.knowledge_mode",
        )
    if default is None:
        return None
    return _normalize_mode(default, source="default knowledge mode")


def _normalize_mode(value: object, *, source: str) -> KnowledgeMode:
    if not isinstance(value, str):
        raise ValueError(f"{source} must be 'off' or 'on'; got {value!r}")

    normalized = value.strip().lower()
    if normalized not in _VALID_MODES:
        raise ValueError(f"{source} must be 'off' or 'on'; got {value!r}")
    return cast(KnowledgeMode, normalized)
