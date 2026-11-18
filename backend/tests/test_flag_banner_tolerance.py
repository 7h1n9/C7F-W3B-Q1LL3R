"""A flag's banner is a hint, not a filter.

A challenge's declared ``flag_pattern`` is operator input and is routinely wrong:
left at the ``flag`` default while the challenge actually uses ``CTF2{...}``, or
carrying a banner typo.  Before this, a strict full-match meant such a flag was
never even recorded as a candidate, so the run could not solve no matter what the
worker recovered.  These tests pin the widened acceptance AND the noise floor that
keeps page source from turning into candidates.
"""

from __future__ import annotations

import pytest

from app.services.flags import FlagService, _matches_declared
from muteki.solver.gate import GENERIC_FLAG_RE, flag_ok, matches_flag_format

DECLARED = r"flag\{[^}]+\}"


@pytest.mark.parametrize(
    "candidate",
    [
        "flag{real_one}",
        "CTF2{d63ea4dc-b6e0-4cd4-ba47-15c1a3514623}",
        "XXCTF{hello_world}",
        "XXX{abc123}",
        "dasctf{lowercase_banner}",
        "picoCTF{abc}",
        "DASCTF{2026}",
    ],
)
def test_every_flag_shaped_banner_is_accepted(candidate: str) -> None:
    assert matches_flag_format(candidate, DECLARED)
    assert FlagService._is_displayable(candidate, DECLARED)


@pytest.mark.parametrize(
    "noise",
    [
        "body{color:red}",     # CSS
        "if{x}",               # JS
        "div{display:none}",   # CSS
    ],
)
def test_ordinary_source_code_is_not_mistaken_for_a_flag(noise: str) -> None:
    assert not matches_flag_format(noise, DECLARED)
    assert not FlagService._is_displayable(noise, DECLARED)


def test_extraction_scans_both_the_declared_pattern_and_the_generic_shape() -> None:
    content = (
        "css body{color:red} then CTF2{d63ea4dc-b6e0-4cd4} "
        "and flag{real_one} plus XXCTF{win}"
    )
    assert FlagService._extract_matches(DECLARED, content) == [
        "flag{real_one}",
        "CTF2{d63ea4dc-b6e0-4cd4}",
        "XXCTF{win}",
    ]


def test_a_wrong_declared_banner_still_yields_the_real_flag() -> None:
    """The exact regression: a challenge configured as ``flag`` but really CTF2."""

    content = "submit CTF2{d63ea4dc-b6e0-4cd4-ba47-15c1a3514623} to /submit"
    assert FlagService._extract_matches(DECLARED, content) == [
        "CTF2{d63ea4dc-b6e0-4cd4-ba47-15c1a3514623}"
    ]


def test_declared_match_is_recorded_separately_from_generic_acceptance() -> None:
    """The UI must be able to tell "your banner is wrong" from "this is your flag"."""

    assert _matches_declared("flag{x}", DECLARED)
    assert not _matches_declared("CTF2{x}", DECLARED)
    # ...yet the generic shape still accepts it.
    assert matches_flag_format("CTF2{x}", DECLARED)


def test_the_provenance_moat_is_unchanged() -> None:
    """Widening the FORMAT must not widen the provenance requirement."""

    class _Store:
        def read_text(self, aid: str) -> str:  # noqa: ARG002
            return ""

    # Right shape, but the candidate never appears in real output.
    assert not flag_ok("CTF2{hallucinated}", "no such text", flag_format=DECLARED, artifacts=_Store())
    # Right shape AND present verbatim in real output.
    assert flag_ok("CTF2{real}", "found CTF2{real} here", flag_format=DECLARED, artifacts=_Store())


@pytest.mark.parametrize("placeholder", ["flag{...}", "flag{uuid}", "flag{FLAG}", "<flag>"])
def test_placeholders_are_still_rejected_after_widening(placeholder: str) -> None:
    class _Store:
        def read_text(self, aid: str) -> str:  # noqa: ARG002
            return ""

    assert not flag_ok(
        placeholder,
        f"the worker printed {placeholder} as a template",
        flag_format=DECLARED,
        artifacts=_Store(),
    )


def test_generic_shape_is_anchored_so_it_cannot_span_text() -> None:
    # Multi-brace / quoted / newline-bearing blobs must never match.
    for blob in ('flag{a}flag{b}', 'flag{"a"}', "flag{a\nb}", "flag{a\\b}"):
        assert GENERIC_FLAG_RE.fullmatch(blob) is None
