"""Protocol switch: bidirectional Chat Completions / Responses / Messages.

Run it with::

    python -m protocol_switch

and point any model config at ``http://127.0.0.1:8790/v1``.  See
``protocol_switch.config`` for the environment variables.
"""

from .config import Settings
from .server import app, create_app
from .translate import (
    CHAT,
    MESSAGES,
    RESPONSES,
    Answer,
    Canonical,
    Params,
    ToolCall,
    ToolSpec,
    Turn,
    decode,
    encode,
    parse_answer,
    render,
    resolve_endpoint,
)

__all__ = [
    "CHAT",
    "MESSAGES",
    "RESPONSES",
    "Answer",
    "Canonical",
    "Params",
    "Settings",
    "ToolCall",
    "ToolSpec",
    "Turn",
    "app",
    "create_app",
    "decode",
    "encode",
    "parse_answer",
    "render",
    "resolve_endpoint",
]

__version__ = "1.0.0"
