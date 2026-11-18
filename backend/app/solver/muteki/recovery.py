"""Recover a flag when the Run ran out of time holding only the *value*.

The situation this exists for, measured on the real-class arena boxes
(``0ab61266`` / ``879d42b0`` / ``87e96c2e``): a Worker recovers the payload of a
flag but never the arena's own banner.  Each of those Runs ended holding a fact
like ``FOUND_FLAG=n1book{afr_1_solved}`` whose **inner value** was genuine while
the wrapper came from the box's own source, so the challenge pattern
(``CTF2\\{[^}]+\\}``) refused it and the Run closed UNSOLVED with the value
thrown away.

The value is worth keeping even when the wrapper is wrong: the banner is the
one part of a flag the arena tells us up front, and the value is the one part
only the box can tell us.  Combining them is not a guess -- it is assembling a
candidate from the two authoritative halves, then handing it to the **same**
gate as any other candidate.

What this module deliberately does not do:

* It does not widen the gate.  A re-wrapped candidate still has to survive
  ``flag_ok`` -- exact pattern match, placeholder rejection, and presence in real
  output.  This module only supplies shape; it never supplies acceptance.
* It does not invent a value.  Every candidate comes from text a Worker already
  recovered.  No value, no candidate.
* It does not run before the clock expires.  It is the *post-convergence*
  fallback, so a Run with time left keeps looking for the real banner instead of
  spending attempts on a synthesised one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Any ``HEAD{value}``-shaped token in prose, whatever its banner.
#:
#: Deliberately banner-agnostic: the whole point is that the box's banner is the
#: wrong one.  The value is restricted to characters that can appear inside a
#: flag (no braces, no whitespace, no quotes) so a stray ``{...}`` from JSON, CSS
#: or a template never becomes a candidate.
_WRAPPED_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z][A-Za-z0-9_]{1,31})\{([^{}\s\"'`\\]{4,256})\}")

#: A bare value is only considered when it is *labelled* as the flag, because an
#: unlabelled random string cannot be distinguished from any other output.
#:
#: The character class stops at ``{`` so the match cannot creep backwards into the
#: prefix of ``n1book{...}``; ``FOUND_FLAG=n1book{...}`` is handled entirely by
#: ``_WRAPPED_RE`` and must not also yield a bare ``n1book``.
_LABELLED_RE = re.compile(
    r"(?:FOUND_FLAG|FLAG|flag|答案|结果)\s*[:=：]\s*([A-Za-z0-9_\-./+=]{4,256})"
)

#: Bars that make a captured value obviously not a flag.
_REJECT_VALUE = re.compile(r"^(?:https?|true|false|null|none|undefined)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One value recovered from Worker output, with the banner it arrived in."""

    value: str
    banner: str
    source: str

    @property
    def as_found(self) -> str:
        return f"{self.banner}{{{self.value}}}"


def banner_of(flag_pattern: str | None, *, default: str = "flag") -> str:
    """The literal prefix a challenge's pattern requires.

    ``CTF2\\{[^}]+\\}`` -> ``CTF2``.  Anything the pattern cannot disclose
    (a fully generic pattern, an unusable expression) falls back to ``flag``,
    which is what ``DEFAULT_FLAG_PATTERN`` uses.
    """

    text = str(flag_pattern or "").strip()
    if not text:
        return default
    # The stored form is regex, so the brace is ``\{``; a bare ``{`` is legal too.
    match = re.match(r"^([A-Za-z0-9_\-]{1,32}?)(?:\\\{|\{)", text)
    if match:
        return match.group(1)
    return default


def _looks_like_a_value(value: str) -> bool:
    text = str(value or "").strip()
    if len(text) < 4 or len(text) > 256:
        return False
    if _REJECT_VALUE.match(text):
        return False
    # A value that is still carrying its own escapes did not survive a shell.
    if any(char in text for char in "{}\\ \t\r\n\"'"):
        return False
    return True


def candidates_from_text(text: str, *, source: str = "") -> list[Candidate]:
    """Every flag-shaped value in ``text``, in the order it appears."""

    out: list[Candidate] = []
    seen: set[str] = set()
    #: Values that arrived wrapped; a bare match of the same prefix is the same
    #: token, not a second candidate.
    wrapped_prefixes: set[str] = set()
    blob = str(text or "")
    if not blob:
        return out
    for match in _WRAPPED_RE.finditer(blob):
        banner, value = match.group(1), match.group(2)
        if not _looks_like_a_value(value):
            continue
        wrapped_prefixes.add(banner)
        if value in seen:
            continue
        seen.add(value)
        out.append(Candidate(value=value, banner=banner, source=source))
    for match in _LABELLED_RE.finditer(blob):
        value = match.group(1).strip(".,;:)]}")
        # ``FOUND_FLAG=n1book{...}`` labels the *wrapped* form already captured
        # above; the bare match would only repeat its banner as a "value".
        if value in wrapped_prefixes:
            continue
        if not _looks_like_a_value(value) or value in seen:
            continue
        seen.add(value)
        out.append(Candidate(value=value, banner="", source=source))
    return out


def wrap_with_banner(candidate: Candidate, flag_pattern: str | None) -> list[str]:
    """The candidate under the arena's banner, then under its own, in that order.

    Arena banner first: it is the only wrapper the gate will accept when the
    value is right.  The candidate's original wrapper is kept second so a Run
    whose ``flag_pattern`` was mis-registered still gets a chance.
    """

    wanted = banner_of(flag_pattern)
    spellings: list[str] = [f"{wanted}{{{candidate.value}}}"]
    if candidate.banner and candidate.banner != wanted:
        spellings.append(candidate.as_found)
    return spellings


__all__ = [
    "Candidate",
    "banner_of",
    "candidates_from_text",
    "wrap_with_banner",
]
