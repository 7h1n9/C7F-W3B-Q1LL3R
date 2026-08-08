from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..graph import Intent, MutekiGraph
from .engine import WorkerEngine, WorkerResult


@dataclass(frozen=True, slots=True)
class PocMaterialization:
    poc_id: str
    path: str
    content_length: int


class PocWorker:
    """Load a graph-backed PoC into a workspace before optional execution."""

    def __init__(self, graph: MutekiGraph, *, worker_id: str = "poc-worker") -> None:
        self.graph = graph
        self.worker_id = worker_id

    def materialize(self, poc_id: str, workspace: str) -> PocMaterialization | None:
        poc = self.graph.get_poc(poc_id)
        if poc is None:
            return None
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", poc.poc_id)[:120] or "poc"
        root = Path(workspace).resolve()
        root.mkdir(parents=True, exist_ok=True)
        target = (root / "pocs" / f"{safe_id}.txt").resolve()
        if root not in target.parents:
            raise ValueError("POC_WORKSPACE_ESCAPE")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(poc.content, encoding="utf-8")
        self.graph.add_fact(
            actor=self.worker_id,
            content=f"POC_MATERIALIZED poc_id={poc.poc_id}; path={target.relative_to(root).as_posix()}",
            verified=False,
            dedupe_key=f"poc:materialized:{poc.poc_id}",
        )
        return PocMaterialization(poc.poc_id, str(target), len(poc.content))

    async def execute(self, intent: Intent, workspace: str, engine: WorkerEngine | None = None) -> WorkerResult:
        poc_id = str((intent.payload or {}).get("poc_id") or "")
        materialized = self.materialize(poc_id, workspace)
        if materialized is None:
            return WorkerResult(False, "poc", metadata={"reason": "POC_NOT_FOUND", "poc_id": poc_id})
        if engine is None:
            return WorkerResult(True, "poc", metadata={"status": "MATERIALIZED", "poc_id": poc_id, "path": materialized.path})
        return await engine.execute(intent, workspace)


__all__ = ["PocMaterialization", "PocWorker"]
