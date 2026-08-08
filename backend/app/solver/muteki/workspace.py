from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .graph import MutekiGraph
from .workers import EngineProfile


@dataclass(slots=True)
class MutekiWorkspace:
    """Prepare a per-challenge filesystem without copying the repository."""

    root: Path
    challenge_id: str
    graph: MutekiGraph
    engines: list[EngineProfile]

    @classmethod
    def prepare(
        cls,
        root: str | Path,
        challenge_id: str,
        *,
        attachments: Iterable[str | Path] = (),
        engines: Iterable[EngineProfile] = (),
        event_subscriber=None,
    ) -> "MutekiWorkspace":
        workspace = Path(root) / challenge_id
        graph_dir = workspace / "graph"
        (workspace / "attachments").mkdir(parents=True, exist_ok=True)
        (workspace / "tmp").mkdir(parents=True, exist_ok=True)
        graph = MutekiGraph(graph_dir / "shared_graph.db", challenge_id=challenge_id, event_subscriber=event_subscriber)
        for source in attachments:
            path = Path(source)
            if path.is_file():
                shutil.copy2(path, workspace / "attachments" / path.name)
        profiles = list(engines)
        for profile in profiles:
            graph.emit_event(actor="coordinator", event_type="prepare.engine.checked", payload={"engine_id": profile.engine_id, "healthy": profile.healthy})
        return cls(workspace, challenge_id, graph, profiles)

    def available_engines(self) -> list[EngineProfile]:
        return [profile for profile in self.engines if profile.healthy]

    def close(self) -> None:
        self.graph.close()
