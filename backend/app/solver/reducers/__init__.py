"""Observation reducers for the isolated Solver Core."""

from .base import KnowledgeUpdate, ObservationReducer
from .web import WebObservationReducer

__all__ = ["KnowledgeUpdate", "ObservationReducer", "WebObservationReducer"]
