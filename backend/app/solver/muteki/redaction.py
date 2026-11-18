"""Redaction for Worker command/result observability.

The Coordinator used to publish only ``kind``/``tool`` for every Worker step,
which made a failed round impossible to review after the fact: the event stream
said *"a shell tool ran"* and nothing about **which** command or **what** it
returned.  Recording the command is what turns ``worker_step`` into something a
human can actually audit.

The reason the original contract withheld it was credential leakage, and that
concern is real: targets echo back ``Set-Cookie`` headers, ``Authorization``
values, database DSNs and one-time tokens.  So the text is *not* stored raw --
it goes through :func:`redact` first, which masks the common secret shapes and
bounds the size.

Two deliberate non-goals:

* **Flags are never masked.**  A flag in tool output is the single most useful
  thing in the trace; masking it would defeat the purpose.
* **The command is not normalised.**  Byte-for-byte fidelity is what makes a
  replay possible, so only secret *values* are replaced, never the syntax.
"""

from __future__ import annotations

import re

# Upper bounds chosen so one event stays well under the row-size limits while
# still holding a full one-liner (a curl with headers) and a useful slice of
# output.  ``python_run`` payloads are the long tail: their script body is
# bounded separately at the tool boundary, so this only clips the preview.
#
# COMMAND_LIMIT was 900 and that is measurably too small: the payloads worth
# recording are exactly the long ones.  A Struts2 OGNL expression with the
# ProcessBuilder trampoline, a HugeGraph gremlin statement, or a heredoc'd
# exploit script all run to 1-3 KB, so at 900 they were cut mid-statement and
# unusable for replay.  Ironic detail measured 2026-11-05: almost nothing ever
# reached 900, because the parser upstream clipped commands to 300 first — the
# limit was too tight *and* mostly unreachable.  The column is MySQL ``json``
# (no 64 KB row cap) and the largest event on record is 7.7 KB, so 4000 leaves
# plenty of headroom while keeping one row cheap.
COMMAND_LIMIT = 4000
RESULT_LIMIT = 1200

_REDACTED = "[redacted]"

# ``key = value`` / ``key: value`` in headers, DSLs, a=1&b=2 query strings and
# shell ``KEY=value`` assignments.  The value class deliberately stops at
# whitespace, quotes, ``&``, ``;`` and ``,`` so a redaction never eats the rest
# of a command line.
_SECRET_KV = re.compile(
    r"(?i)\b("
    r"pass(?:word|wd|phrase)?|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|auth|authorization|cookie|set-cookie|session(?:id)?|"
    r"credential|dsn"
    r")(\s*[:=]\s*)(?!\[redacted\])([^\s\"'&,;]{3,})"
)

# ``Authorization: Bearer <jwt>`` and bare bearer tokens.
_BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-+/=]{12,}")

# URL userinfo: ``scheme://user:pass@host`` -> ``scheme://user:[redacted]@host``
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^/\s:@]+:)([^/\s@]{3,})@")


def redact(text: object, *, limit: int = COMMAND_LIMIT) -> str:
    """Return ``text`` with secret values masked and bounded to ``limit``.

    Accepts anything: a ``None`` becomes ``""`` and non-strings are coerced, so
    callers on the Worker's hot path never have to type-guard first.
    """

    if text is None:
        return ""
    value = text if isinstance(text, str) else str(text)
    if not value:
        return ""
    value = _BEARER.sub(r"\1 " + _REDACTED, value)
    value = _URL_USERINFO.sub(r"\1" + _REDACTED + "@", value)
    value = _SECRET_KV.sub(r"\1\2" + _REDACTED, value)
    if len(value) > limit:
        omitted = len(value) - limit
        value = value[:limit] + f"…[+{omitted} chars]"
    return value


__all__ = ["COMMAND_LIMIT", "RESULT_LIMIT", "redact"]
