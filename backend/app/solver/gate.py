"""Compatibility exports for the multi-worker flag acceptance boundary."""

from .shared_graph.gate import FlagGate, GateDecision

__all__ = ["FlagGate", "GateDecision"]
