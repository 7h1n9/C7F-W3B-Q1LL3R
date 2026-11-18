from __future__ import annotations

from .cli import CliWorkerEngine


class ClaudeEngine(CliWorkerEngine):
    """Claude Code headless Worker.

    The vendored ``ClaudeCodeDriver`` owns the argv: the official non-interactive
    worker permission flag (``--dangerously-skip-permissions``), the JSON/JSONL
    output format, and -- when a caller turns those surfaces off -- Web-tool
    denial (``--disallowed-tools WebSearch WebFetch``) plus MCP isolation
    (``--mcp-config '{"mcpServers":{}}' --strict-mcp-config``).  Parsing the
    result envelope is what makes the session id, turn count and cache-token
    buckets available to the Blackboard; the previous hand-written argv produced
    only raw stdout.
    """

    engine = "claude"


__all__ = ["ClaudeEngine"]
