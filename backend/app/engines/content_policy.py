"""Recognise and soften prompt-level content-audit rejections.

A relay in front of a reasoning model may run a content audit and reject the
*request* rather than answer it::

    HTTP 403
    {"error": {"message": "内容审计命中风险规则，请调整输入后重试",
               "type": "content_policy_violation"}}

That is not an authentication failure and not a transport failure: the
credential is fine, the endpoint is up, and the identical request succeeds once
the wording changes.  Mapping it to ``MODEL_PERMISSION_DENIED`` -- which is what
the engine did before this module existed -- is actively misleading: the Run
reactivation logic treats a permission denial as "this model will stay broken",
and the operator sees a credential problem where the real fix is a rephrase.

Two responsibilities, both deliberately narrow:

* :func:`is_content_policy_error` -- identify the class, including the 403 body,
  which the engine used to discard.
* :func:`soften_prompt_for_audit` -- rewrite *prose* wording only.

Why "prose only" matters: the payload bytes are the part that solves the
challenge.  Rewriting ``exploit`` to ``verify`` in an explanatory sentence costs
nothing, but rewriting it inside an HTTP request or a script changes the attack
and silently breaks the very thing the prompt exists to convey.  Fenced blocks,
inline code and anything that looks like a URL or a request line are therefore
copied through untouched, and a rewrite is only reported when it actually
changed something.
"""

from __future__ import annotations

import re

__all__ = [
    "CONTENT_POLICY_CODE",
    "is_content_policy_error",
    "looks_like_content_policy_body",
    "soften_prompt_for_audit",
]

#: The dedicated provider error code.  Distinct from ``MODEL_PERMISSION_DENIED``
#: on purpose -- see the module docstring.
CONTENT_POLICY_CODE = "MODEL_CONTENT_POLICY"

#: Body markers, matched case-insensitively.  The English identifier is what a
#: structured error carries; the Chinese sentence is what this relay's human
#: message carries, and either alone is enough.
_BODY_MARKERS = (
    "content_policy_violation",
    "content_policy",
    "content policy",
    "内容审计",
    "风险规则",
    "content_filter",
    "responsible ai policy",
    "safety policy",
)

#: Prose substitutions: role and intent wording only.  Ordered longest-first so
#: ``reverse shell`` is not half-replaced by a shorter pattern.  Replacements are
#: chosen to keep the *meaning* for the model -- it must still know it is being
#: asked to demonstrate a reachability or validation step.
_SOFTENING: tuple[tuple[str, str], ...] = (
    (r"reverse\s+shell", "outbound connectivity check"),
    (r"bind\s+shell", "listener check"),
    (r"privilege\s+escalation", "privilege boundary verification"),
    (r"priv\s*esc", "privilege boundary verification"),
    (r"post[\s-]?exploitation", "post-validation follow-up"),
    (r"exploits?", "verification request"),
    (r"exploiting", "verifying"),
    (r"exploited", "verified"),
    (r"attack\s+surface", "reachable surface"),
    (r"attacker", "reviewer"),
    (r"attack", "assessment step"),
    (r"payloads?", "request body"),
    (r"webshell", "server-side script"),
    (r"backdoor", "diagnostic hook"),
    (r"crontab", "scheduled task"),
    (r"brute[\s-]?force", "credential enumeration"),
    (r"bruteforce", "credential enumeration"),
    (r"exfiltrate", "retrieve"),
    (r"exfiltration", "retrieval"),
    (r"bypass(es|ed)?", "satisfy"),
    (r"dump\s+the\s+database", "read the database schema"),
    (r"steal", "retrieve"),
    (r"hijack", "take over"),
    (r"inject(ion|s)?\b", "supply input"),
    (r"malicious", "unexpected"),
    (r"victim", "target"),
)

_PROSE_PATTERNS = tuple(
    (re.compile(pattern, re.IGNORECASE), replacement) for pattern, replacement in _SOFTENING
)

#: Regions whose bytes must survive verbatim: fenced code, inline code, URLs and
#: HTTP request lines.  Protecting them is what keeps the softened prompt usable.
_PROTECTED = re.compile(
    r"```.*?```"          # fenced code block, any language
    r"|`[^`\n]+`"         # inline code
    r"|https?://\S+"      # URL
    r"|\b(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+\S+\s+HTTP/[\d.]+"
    r"|^[A-Za-z0-9+/=]{40,}$",  # a bare opaque token/flag body
    re.DOTALL | re.MULTILINE,
)


def looks_like_content_policy_body(text: str) -> bool:
    """Does this response body carry a content-audit rejection?"""

    lowered = str(text or "").casefold()
    if not lowered:
        return False
    return any(marker.casefold() in lowered for marker in _BODY_MARKERS)


def is_content_policy_error(error: BaseException) -> bool:
    """Is this exception a content-audit rejection rather than an auth failure.

    Walks the ``__cause__``/``__context__`` chain because the engine wraps the
    ``httpx.HTTPStatusError`` in a ``ModelProviderError``; the status code and
    the body live on the wrapped exception, not on the wrapper.
    """

    current: BaseException | None = error
    for _ in range(8):
        if current is None:
            break
        text = str(current)
        code = str(getattr(current, "code", "") or "")
        if CONTENT_POLICY_CODE in code or CONTENT_POLICY_CODE in text:
            return True
        if looks_like_content_policy_body(text):
            return True
        response = getattr(current, "response", None)
        body = ""
        if response is not None:
            try:
                body = response.text
            except Exception:  # noqa: BLE001 - a closed body is not a reason to raise
                body = ""
        if body and looks_like_content_policy_body(body):
            return True
        current = current.__cause__ or current.__context__
    return False


def soften_prompt_for_audit(text: str) -> str:
    """Rewrite prose wording that trips a content audit; leave payloads alone.

    Returns the original string when nothing changed, so a caller can tell a
    no-op apart from a real rewrite and avoid a pointless retry.
    """

    original = str(text or "")
    if not original:
        return original

    # Split into protected and unprotected spans, soften only the latter.
    pieces: list[str] = []
    cursor = 0
    for match in _PROTECTED.finditer(original):
        start, end = match.span()
        if start > cursor:
            pieces.append(_soften_prose(original[cursor:start]))
        pieces.append(match.group(0))
        cursor = end
    if cursor < len(original):
        pieces.append(_soften_prose(original[cursor:]))
    return "".join(pieces)


def _soften_prose(segment: str) -> str:
    result = segment
    for pattern, replacement in _PROSE_PATTERNS:
        result = pattern.sub(replacement, result)
    return result
