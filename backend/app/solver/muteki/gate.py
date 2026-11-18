from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GateDecision:
    accepted: bool
    reason: str
    flag: str


DEFAULT_FLAG_PATTERN = r"flag\{[^{}\r\n]+\}"


def compile_flag_pattern(pattern: str | None) -> re.Pattern[str]:
    """Compile a challenge flag pattern, falling back to the default shape.

    The pattern arrives from operator input, so an unusable expression must
    degrade to the historical default instead of disabling the gate.
    """

    text = str(pattern or "").strip()
    if text:
        try:
            return re.compile(text)
        except re.error:
            pass
    return re.compile(DEFAULT_FLAG_PATTERN)


class MutekiFlagGate:
    """The single flag acceptance boundary."""

    _pattern = re.compile(DEFAULT_FLAG_PATTERN)
    _placeholders = frozenset({"flag{test}", "flag{placeholder}", "flag{dummy}"})

    #: Placeholder *values*, checked independently of the banner.
    #:
    #: ``_placeholders`` compares whole strings, so ``CTF2{test}`` slipped past a
    #: set that only listed ``flag{test}``.  The recovery path made that matter:
    #: it can re-wrap any value under the challenge banner, so a Worker writing
    #: ``n1book{test}`` would have produced an accepted ``CTF2{test}``.
    _placeholder_values = frozenset(
        {"test", "placeholder", "dummy", "example", "sample", "flag", "changeme", "todo"}
    )

    def _is_placeholder_value(self, candidate: str, recovered_value: str = "") -> bool:
        value = str(recovered_value or "").strip()
        if not value:
            match = re.match(r"^[^{]*\{([^{}]*)\}$", str(candidate or "").strip())
            value = match.group(1) if match else ""
        return value.strip().casefold() in self._placeholder_values

    def __init__(self, flag_pattern: str | None = None) -> None:
        # The challenge owns its flag shape (``CTF2{...}``, ``flag{...}``, ...).
        # Hardcoding one prefix silently rejected every challenge that used a
        # different banner, so the format now travels with the gate.
        self.flag_pattern = str(flag_pattern or "").strip() or DEFAULT_FLAG_PATTERN
        self._pattern = compile_flag_pattern(self.flag_pattern)

    def verify(self, flag: str, *, real_output: str) -> GateDecision:
        candidate = str(flag).strip()
        if not self._pattern.fullmatch(candidate):
            return GateDecision(False, "FORMAT_INVALID", candidate)
        if candidate.casefold() in self._placeholders or self._is_placeholder_value(candidate):
            return GateDecision(False, "PLACEHOLDER", candidate)
        if candidate not in str(real_output):
            return GateDecision(False, "NOT_IN_REAL_OUTPUT", candidate)
        return GateDecision(True, "ACCEPTED", candidate)

    def verify_recovered(
        self,
        flag: str,
        *,
        real_output: str,
        recovered_value: str,
    ) -> GateDecision:
        """Verify a candidate whose **banner was supplied by us**, not read.

        ``verify`` demands the whole string appear in the output, which is the
        right check for a Worker that claims to have *read* a flag: it is what
        stops a hallucinated token from becoming a solve.

        A recovered candidate is a different situation.  Only the value ever
        appeared in the output; the banner is ours by definition, so the full
        string can never be found there and ``verify`` would reject every
        correct recovery.  The evidence requirement is therefore moved onto the
        part the box actually produced -- the value -- and stays exactly as
        strict: the value must appear verbatim in the recorded output.

        The pattern is still enforced on the full string, so a value that cannot
        be expressed under this challenge's banner is still refused, and the
        placeholder check still applies.
        """

        candidate = str(flag).strip()
        if not self._pattern.fullmatch(candidate):
            return GateDecision(False, "FORMAT_INVALID", candidate)
        if candidate.casefold() in self._placeholders or self._is_placeholder_value(
            candidate, recovered_value
        ):
            return GateDecision(False, "PLACEHOLDER", candidate)
        value = str(recovered_value or "").strip()
        if not value:
            return GateDecision(False, "NO_RECOVERED_VALUE", candidate)
        if value not in str(real_output):
            # The value itself must be witnessed; without that this is a guess.
            return GateDecision(False, "NOT_IN_REAL_OUTPUT", candidate)
        return GateDecision(True, "ACCEPTED_RECOVERED", candidate)
