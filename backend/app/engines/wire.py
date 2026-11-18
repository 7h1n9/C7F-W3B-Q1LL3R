"""Backend-facing view of the shared OpenAI wire helpers.

The implementation lives in ``muteki.core.wire`` because the vendored core
(``muteki.core.llm``, which the container-side Worker uses) needs the same
translation and must never import ``app.*``.  Re-exporting keeps one
implementation, so the settings probe and the live Worker cannot drift.
"""

from muteki.core.wire import (
    CHAT_COMPLETIONS,
    RESPONSES,
    WIRE_API_VALUES,
    normalize_wire_api,
    resolve_endpoint,
    responses_to_chat_body,
    strip_endpoint_path,
    to_responses_request,
)

__all__ = [
    "CHAT_COMPLETIONS",
    "RESPONSES",
    "WIRE_API_VALUES",
    "normalize_wire_api",
    "resolve_endpoint",
    "responses_to_chat_body",
    "strip_endpoint_path",
    "to_responses_request",
]
