"""Load and validate curated knowledge items from JSONL files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.knowledge.models import KnowledgeItem

DEFAULT_KNOWLEDGE_ROOT = Path(__file__).resolve().parents[3] / "data" / "knowledge"


class KnowledgeLoader:
    """Load stable-sorted JSONL knowledge files from a file or directory."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else DEFAULT_KNOWLEDGE_ROOT

    def load(self) -> list[KnowledgeItem]:
        paths = self._resolve_paths()
        items: list[KnowledgeItem] = []
        seen_ids: dict[str, str] = {}

        for path in paths:
            for line_number, item in self._iter_file(path):
                location = f"{path}:{line_number}"
                self._validate_cross_fields(item, location)

                previous = seen_ids.get(item.id)
                if previous is not None:
                    raise ValueError(
                        f"{location}: duplicate knowledge id {item.id!r}; "
                        f"first defined at {previous}"
                    )
                seen_ids[item.id] = location
                items.append(item)

        return items

    def _resolve_paths(self) -> list[Path]:
        root = self.root.expanduser().resolve()
        if root.is_file():
            if root.suffix.lower() != ".jsonl":
                raise ValueError(f"{root}: knowledge file must use the .jsonl suffix")
            return [root]

        if not root.exists():
            raise FileNotFoundError(f"Knowledge root does not exist: {root}")
        if not root.is_dir():
            raise ValueError(f"Knowledge root is not a directory or JSONL file: {root}")

        paths = sorted(root.glob("*.jsonl"), key=lambda path: path.name)
        if not paths:
            raise ValueError(f"No *.jsonl knowledge files found under: {root}")
        return paths

    def _iter_file(self, path: Path):
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line_number, raw_line in enumerate(handle, start=1):
                    stripped = raw_line.strip()
                    if not stripped:
                        continue
                    yield line_number, self._parse_line(path, line_number, stripped)
        except OSError as exc:
            raise ValueError(f"Unable to read knowledge file {path}: {exc}") from exc

    def _parse_line(self, path: Path, line_number: int, raw_line: str) -> KnowledgeItem:
        location = f"{path}:{line_number}"
        try:
            payload: Any = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{location}: invalid JSON at column {exc.colno}: {exc.msg}"
            ) from exc

        if not isinstance(payload, dict):
            raise ValueError(f"{location}: each JSONL line must contain a JSON object")

        try:
            return KnowledgeItem.model_validate(payload)
        except ValidationError as exc:
            raise ValueError(f"{location}: invalid knowledge item: {exc}") from exc

    @staticmethod
    def _validate_cross_fields(item: KnowledgeItem, location: str) -> None:
        for field_name, value in (
            ("id", item.id),
            ("title", item.title),
            ("technique", item.technique),
            ("summary", item.summary),
            ("provenance.source_type", item.provenance.source_type),
            ("provenance.source_ref", item.provenance.source_ref),
        ):
            if not value.strip():
                raise ValueError(f"{location}: {field_name} must not be empty")

        for field_name, values in (
            ("preconditions", item.preconditions),
            ("target_stack", item.target_stack),
            ("verification_method", item.verification_method),
            ("known_failures", item.known_failures),
            ("steps", item.steps),
        ):
            if any(not value.strip() for value in values):
                raise ValueError(f"{location}: {field_name} must not contain blank entries")

        if not item.steps:
            raise ValueError(f"{location}: steps must contain at least one executable step")
        if not item.verification_method:
            raise ValueError(f"{location}: verification_method must not be empty")
        # ``negative`` and ``case`` both assert something about a *route* rather
        # than a single technique, so an entry of either kind is only useful if
        # it names the conditions under which the route stops working.  Without
        # ``known_failures`` a recalled prior reads as "this always works" and is
        # exactly the kind of over-confident hint the recall path is meant to
        # avoid.
        if item.kind in {"negative", "case"} and not item.known_failures:
            raise ValueError(
                f"{location}: {item.kind} knowledge requires known_failures"
            )


def load_knowledge_items(root: str | Path | None = None) -> list[KnowledgeItem]:
    """Load all stable-sorted *.jsonl items under root, or the repo corpus by default."""

    return KnowledgeLoader(root).load()
