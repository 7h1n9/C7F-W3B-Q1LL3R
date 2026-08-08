from __future__ import annotations

from enum import StrEnum


class WorkerRole(StrEnum):
    RACE = "race"
    BOOTSTRAP = "bootstrap"
    CLASSIFIER = "classifier"
    EXPLORE = "explore"
    REVIEW = "review"


__all__ = ["WorkerRole"]
