"""Metadata-only adapter for DASCTF Agent arena challenges.

Follows the same contract as :mod:`app.challenge_adapters.asset_warranty`: it
describes stage names and request constraints, and reads every target-specific
value from ``Challenge.metadata_json``.  It never carries a flag, an answer, or a
column name -- an arena challenge's ground truth lives on the platform, and an
adapter that cached it would turn a rehearsal into a lookup.

The arena differs from the local range in one way that matters to the stages: the
target address is chosen by the platform each time an environment starts, so an
adapter cannot pin a URL.  ``fresh_verification_required`` is therefore not a
preference here but a consequence -- a remembered address proves nothing.
"""

from __future__ import annotations

from typing import Any

from app.models.challenge import Challenge

#: Stages of an arena engagement.  ``environment_lease`` precedes recon because
#: the arena grants one environment at a time and a challenge that needs a live
#: target cannot be worked on until that lease is held.
ARENA_STAGES = [
    "environment_lease",
    "surface_recon",
    "hypothesis",
    "exploitation",
    "flag_verification",
]


class DasctfArenaAdapter:
    key = "dasctf_arena"

    @staticmethod
    def matches(challenge: Challenge) -> bool:
        metadata = challenge.metadata_json or {}
        return str(metadata.get("adapter") or "").lower() == DasctfArenaAdapter.key

    @staticmethod
    def context(challenge: Challenge) -> dict[str, Any]:
        metadata = challenge.metadata_json or {}
        result: dict[str, Any] = {
            "adapter": DasctfArenaAdapter.key,
            "target_url": challenge.target_url,
            "allowed_hosts": list(challenge.allowed_hosts or []),
            "stages": list(ARENA_STAGES),
            "constraints": {
                # One environment at a time platform-wide; the client serialises
                # rather than the adapter, but the constraint is declared here so
                # a planner does not design a parallel plan it cannot execute.
                "single_environment_lease": True,
                # The platform re-chooses the target per environment start, so a
                # result carried over from a previous lease is not evidence.
                "fresh_verification_required": True,
                "write_scope": "read_only",
            },
        }
        for field in ("platform_challenge_id", "friendly_id", "category", "difficulty", "points"):
            value = metadata.get(field)
            if value is not None:
                result[field] = value
        for flag in ("has_attachments", "has_container", "is_solved"):
            value = metadata.get(flag)
            if value is not None:
                result[flag] = bool(value)
        ground = metadata.get("practice_ground_name")
        if ground:
            result["practice_ground_name"] = str(ground)
        return result


def adapter_for(challenge: Challenge) -> DasctfArenaAdapter | None:
    return DasctfArenaAdapter() if DasctfArenaAdapter.matches(challenge) else None
