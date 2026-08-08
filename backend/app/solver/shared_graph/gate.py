from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GateDecision:
    """The result of validating a flag candidate."""

    accepted: bool
    reason_code: str
    flag_value: str


class FlagGate:
    """Non-pluggable acceptance boundary for worker-discovered flags.

    A candidate is accepted only when it matches the configured format, is not
    an obvious placeholder, and occurs in the worker's real output.  The
    output check is intentionally performed by the host side before a flag is
    persisted as verified.
    """

    _PLACEHOLDERS = frozenset({"flag{test}", "flag{placeholder}", "flag{dummy}"})

    def __init__(self, pattern: str = r"flag\{[^{}\r\n]+\}") -> None:
        self._pattern = re.compile(pattern)

    def verify(self, flag_value: str, *, worker_output: str) -> GateDecision:
        value = str(flag_value).strip()
        output = str(worker_output)
        if not self._pattern.fullmatch(value):
            return GateDecision(False, "FLAG_FORMAT_INVALID", value)
        if value.casefold() in self._PLACEHOLDERS:
            return GateDecision(False, "FLAG_PLACEHOLDER", value)
        if value not in output:
            return GateDecision(False, "FLAG_NOT_IN_WORKER_OUTPUT", value)
        return GateDecision(True, "FLAG_ACCEPTED", value)
