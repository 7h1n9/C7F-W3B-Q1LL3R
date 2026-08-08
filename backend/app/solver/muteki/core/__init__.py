"""Canonical Muteki runtime composition and stage policy."""

from .stage_policy import StagePolicy


def __getattr__(name: str):
    if name in {"MutekiOrchestrator", "MutekiRunResult"}:
        from .orchestrator import MutekiOrchestrator, MutekiRunResult

        return {"MutekiOrchestrator": MutekiOrchestrator, "MutekiRunResult": MutekiRunResult}[name]
    raise AttributeError(name)


__all__ = ["MutekiOrchestrator", "MutekiRunResult", "StagePolicy"]
