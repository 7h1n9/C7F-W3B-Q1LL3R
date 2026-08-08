from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GateDecision:
    accepted: bool
    reason: str
    flag: str


class MutekiFlagGate:
    """The single hardcoded flag acceptance boundary."""

    _pattern = re.compile(r"flag\{[^{}\r\n]+\}")
    _placeholders = frozenset({"flag{test}", "flag{placeholder}", "flag{dummy}"})

    def verify(self, flag: str, *, real_output: str) -> GateDecision:
        candidate = str(flag).strip()
        if not self._pattern.fullmatch(candidate):
            return GateDecision(False, "FORMAT_INVALID", candidate)
        if candidate.casefold() in self._placeholders:
            return GateDecision(False, "PLACEHOLDER", candidate)
        if candidate not in str(real_output):
            return GateDecision(False, "NOT_IN_REAL_OUTPUT", candidate)
        return GateDecision(True, "ACCEPTED", candidate)
