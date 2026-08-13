"""Compatibility bridge for the vendored upstream Muteki package.

The project keeps its production adapters under :mod:`app.solver.muteki`.
The upstream package is vendored separately as top-level ``muteki`` so its
official imports remain intact and can be evaluated incrementally.  This
module is deliberately small: it maps the existing Challenge model shape to
the upstream solve-graph model without routing production runs yet.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

UPSTREAM_MUTEKI_VERSION = "0.2.5"

_CATEGORY_BY_CHALLENGE_TYPE = {
    "WEB_TARGET": "web",
    "PWN": "pwn",
    "REVERSE": "reverse",
    "CRYPTO": "crypto",
    "FORENSICS": "forensics",
    "MISC": "misc",
}


def upstream_available() -> bool:
    """Return whether the vendored upstream package can be imported."""

    try:
        from muteki.models.solve_graph import Challenge as _Challenge  # noqa: F401
    except ImportError:
        return False
    return True


def to_upstream_challenge(challenge: Any) -> Any:
    """Convert a current-project challenge into an upstream ``Challenge``.

    Only operator-visible challenge fields are copied.  Runtime-only ORM
    state, database relationships, and challenge metadata are intentionally
    not attached to the upstream graph object.
    """

    from muteki.models.solve_graph import Challenge as UpstreamChallenge

    challenge_type = str(getattr(challenge, "challenge_type", "WEB_TARGET") or "WEB_TARGET")
    category = _CATEGORY_BY_CHALLENGE_TYPE.get(challenge_type.upper(), "web")
    attachments = _attachment_names(getattr(challenge, "attachments", None))

    return UpstreamChallenge(
        id=str(getattr(challenge, "id", "") or ""),
        name=str(getattr(challenge, "name", "") or ""),
        category=category,
        description=str(getattr(challenge, "description", "") or ""),
        attachments=attachments,
        target=str(getattr(challenge, "target_url", "") or "") or None,
        flag_format=str(getattr(challenge, "flag_pattern", r"flag\{.*?\}") or r"flag\{.*?\}"),
    )


def _attachment_names(value: Any) -> list[str]:
    """Return stable attachment names without copying ORM objects."""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []

    names: list[str] = []
    for item in value:
        if isinstance(item, Mapping):
            name = item.get("original_name") or item.get("name") or item.get("path")
        else:
            name = getattr(item, "original_name", None) or getattr(item, "name", None)
        if name:
            names.append(str(name))
    return names


def open_upstream_shared_graph(*, db_path: str, challenge: Any) -> Any:
    """Open the upstream SQLite graph for an already mapped challenge.

    This helper is not called by the current production runtime.  Keeping the
    construction behind one bridge makes the later Coordinator migration
    explicit and prevents upstream persistence from being confused with the
    existing SQLAlchemy Blackboard/Evidence stores.
    """

    from muteki.swarm.shared_graph import SQLiteSharedGraph

    return SQLiteSharedGraph.open(db_path=db_path, challenge=to_upstream_challenge(challenge))
