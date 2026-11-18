from __future__ import annotations

from .cli import CliWorkerEngine


class CursorEngine(CliWorkerEngine):
    """Cursor headless Worker (``cursor-agent``).

    The vendored ``CursorDriver`` owns the argv (``-p --force --trust``, pinned
    model, ``--output-format stream-json``) and the NDJSON parsing, so the chat
    id, token buckets and policy-refusal classification are available to the
    Blackboard instead of being thrown away with the raw stdout.
    """

    engine = "cursor"


__all__ = ["CursorEngine"]
