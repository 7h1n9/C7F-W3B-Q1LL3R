"""Identify the strategy that produced an experiment.

An experiment manifest already pins the benchmark cases, the arms and the
runtime.  It does not pin *the solver strategy itself*: edit
``muteki/strategy.py`` or the Worker's ``AGENTS.md`` discipline text between
two runs and the manifests look identical while the thing under test changed.

``compute_policy_version`` closes that hole by hashing the strategy-bearing
sources into one short, stable id.  Two experiments with the same
``policy_version`` were run against byte-identical strategy code; if the id
differs, the comparison is not paired and the evaluator can say so instead of
quietly averaging two different solvers.

Deliberately dependency-free and path-driven: it hashes files, it does not
import them, so it works in a subprocess, in CI, and in a test with a fake
project root.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable, Mapping

#: Files whose content *is* the policy.  Changing any of them changes what the
#: solver does, so they belong in the id.
POLICY_SOURCES: tuple[str, ...] = (
    # Deterministic planner: which technique is tried next for a given graph.
    "backend/app/solver/muteki/strategy.py",
    # Worker discipline injected as AGENTS.md -- the actual instruction text.
    "backend/app/services/known_exploit.py",
    # Retrieval scoring / PRIOR formatting: what knowledge reaches the Worker.
    "backend/app/knowledge/retriever.py",
    # The transition schema and redaction rules that future stages train on.
    "backend/app/training/transitions.py",
)

#: Corpora that may or may not exist.  A missing corpus is recorded rather
#: than hashed, so an older checkout still yields a comparable id.
OPTIONAL_SOURCES: tuple[str, ...] = (
    "data/knowledge/playbooks/learned-boxes.jsonl",
)

_HASH_CHARS = 12


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()[:_HASH_CHARS]


def compute_policy_version(
    project_root: Path | str | None = None,
    *,
    extra: Mapping[str, str] | None = None,
    required: Iterable[str] = POLICY_SOURCES,
    optional: Iterable[str] = OPTIONAL_SOURCES,
) -> dict:
    """Return ``{policy_version, components, missing}`` for the given root.

    ``extra`` lets a caller fold in non-file knobs (for example
    ``{"knowledge_mode": "on"}``) so an arm difference that lives outside the
    code still shows up in the id.
    """

    root = Path(project_root) if project_root is not None else Path(__file__).resolve().parents[3]
    components: dict[str, str] = {}
    missing: list[str] = []

    for relative in tuple(required) + tuple(optional):
        path = root / relative
        if path.is_file():
            components[relative] = _digest(path)
        else:
            missing.append(relative)

    for key, value in sorted((extra or {}).items()):
        components[f"param:{key}"] = hashlib.sha256(
            str(value).encode("utf-8")
        ).hexdigest()[:_HASH_CHARS]

    seed = "|".join(f"{key}={value}" for key, value in sorted(components.items()))
    version = "pol-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:_HASH_CHARS] if components else "pol-unknown"
    return {
        "policy_version": version,
        "components": components,
        "missing": sorted(missing),
    }
