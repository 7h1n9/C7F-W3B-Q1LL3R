"""One shared reading of "the upstream refused a parameter we sent".

Several callers talk to OpenAI-compatible endpoints -- the Reason planner, the
run writeup, the A-path worker -- and all of them send ``temperature``.
A relay may accept only one value for a given model and answer::

    {"error": {"message": "field temperature invalid, only 1 is allowed for this model",
               "type": "invalid_request_error"}}

That is a fact about *that model*, not a defect in our payload: the same body
works against the next endpoint.  Treating it as a payload defect is what
silently disabled the Reason planner while four healthy configs sat unused.

Keeping the parse in one place matters because the same wording arrives wrapped
differently depending on the caller: :mod:`muteki_llm_report` sees a bare
``httpx.HTTPStatusError``, while the Reason planner sees a
``ModelProviderError`` raised ``from`` it.  One function has to handle both.
"""

from __future__ import annotations

import re

# Fields whose allowed range is decided by the model, not by the request.
NEGOTIABLE_FIELDS = frozenset(
    {
        "temperature",
        "top_p",
        "top_k",
        "max_tokens",
        "max_completion_tokens",
        "presence_penalty",
        "frequency_penalty",
        "repetition_penalty",
        "seed",
        "stop",
        "stream",
        "n",
    }
)

# ``field temperature invalid, only 1 is allowed for this model``.
# The value group is optional: some relays name the field but offer no
# replacement, in which case the caller drops the field entirely.
FIELD_CONSTRAINT_RE = re.compile(
    r"field\s+([A-Za-z_][A-Za-z0-9_]*)\s+invalid"
    r"(?:.*?only\s+(-?\d+(?:\.\d+)?)\s+is\s+allowed)?",
    re.IGNORECASE | re.DOTALL,
)

_CONSTRAINT_STATUSES = frozenset({400, 404, 410, 422})

# An exception chain is walked, so a bounded depth is deliberate: provider
# errors are wrapped once or twice, never hundreds of times.
_MAX_CAUSE_DEPTH = 8


def _http_status_error(error: BaseException | None):
    """Return the first ``httpx.HTTPStatusError`` in the chain, if any.

    ``httpx`` is imported lazily: this module is used from the solver path too
    and must stay importable where ``httpx`` is absent.
    """

    import httpx

    seen = 0
    current: BaseException | None = error
    while current is not None and seen < _MAX_CAUSE_DEPTH:
        if isinstance(current, httpx.HTTPStatusError):
            return current
        current = current.__cause__ or current.__context__
        seen += 1
    return None


def provider_response_body(error: BaseException) -> str:
    """Bounded upstream body for ``error``, or ``""`` when none is reachable."""

    http_error = _http_status_error(error)
    if http_error is None:
        return ""
    return str(getattr(http_error.response, "text", "") or "")


def parameter_constraint(error: BaseException) -> tuple[str, float | None] | None:
    """``(field, allowed_value)`` when the upstream refuses ONE parameter.

    ``allowed_value`` is ``None`` when the message names the field but offers no
    replacement -- the caller then drops the field entirely.
    """

    http_error = _http_status_error(error)
    if http_error is None:
        return None
    if http_error.response.status_code not in _CONSTRAINT_STATUSES:
        return None
    body = str(getattr(http_error.response, "text", "") or "")
    # An HTML body is a proxy/edge error page and carries no parameter name.
    if not body.lstrip().startswith("{"):
        return None
    match = FIELD_CONSTRAINT_RE.search(body)
    if match is None:
        return None
    field = match.group(1).lower()
    if field not in NEGOTIABLE_FIELDS:
        return None
    raw_value = match.group(2)
    if not raw_value:
        return field, None
    try:
        return field, float(raw_value)
    except (TypeError, ValueError):
        return field, None


__all__ = [
    "FIELD_CONSTRAINT_RE",
    "NEGOTIABLE_FIELDS",
    "parameter_constraint",
    "provider_response_body",
]
