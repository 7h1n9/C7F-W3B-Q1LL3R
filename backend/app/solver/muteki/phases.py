from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MutekiPhase(StrEnum):
    PREPARE = "prepare"
    RACE = "race"
    COORDINATOR = "coordinator"
    FINALIZE = "finalize"


@dataclass(frozen=True, slots=True)
class PhaseDecision:
    phase: MutekiPhase
    terminal: bool = False
    reason: str = ""


def next_phase(current: MutekiPhase, *, race_solved: bool = False, stopped: bool = False) -> PhaseDecision:
    if current is MutekiPhase.PREPARE:
        return PhaseDecision(MutekiPhase.RACE)
    if current is MutekiPhase.RACE:
        if race_solved:
            return PhaseDecision(MutekiPhase.FINALIZE, reason="RACE_FLAG_FOUND")
        return PhaseDecision(MutekiPhase.COORDINATOR)
    if current is MutekiPhase.COORDINATOR:
        if stopped:
            return PhaseDecision(MutekiPhase.FINALIZE, reason="OPERATOR_STOP")
        return PhaseDecision(MutekiPhase.COORDINATOR)
    return PhaseDecision(MutekiPhase.FINALIZE, terminal=True, reason="FINALIZED")


__all__ = ["MutekiPhase", "PhaseDecision", "next_phase"]
