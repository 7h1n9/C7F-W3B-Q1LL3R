from __future__ import annotations

from enum import StrEnum


class WorkerRole(StrEnum):
    RACE = "race"
    BOOTSTRAP = "bootstrap"
    EXPLORE = "explore"
    REVIEW = "review"


__all__ = ["WorkerRole"]
