"""Template retrieval (\u00a716) \u2014 recall distilled templates for similar challenges.

The official Muteki ``learning/retrieve.py`` matches the current challenge's
category and keywords against stored templates and returns the best matches as
a PRIOR for the solver, not an answer key.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from muteki.learning.distill import Template, TemplateStore


class TemplateRetriever:
    """Lightweight keyword-match retrieval over the TemplateStore."""

    def __init__(self, store: TemplateStore) -> None:
        self.store = store

    def retrieve(
        self, *, category: str, keywords: list[str], top_k: int = 3
    ) -> list[Template]:
        """Return the ``top_k`` templates most relevant to the given challenge.

        Scoring:
          - exact category match: +3
          - each overlapping keyword: +1
        """
        templates = self.store.load_all()
        scored: list[tuple[int, Template]] = []

        for tpl in templates:
            score = 0
            if tpl.category == category:
                score += 3
            for kw in keywords:
                if kw in tpl.keywords:
                    score += 1
            if score > 0:
                scored.append((score, tpl))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [tpl for _, tpl in scored[:top_k]]

    def format_prior(self, templates: list[Template]) -> str:
        """Format retrieved templates as a compact PRIOR block for the Worker prompt."""
        if not templates:
            return ""

        lines = [
            "## Learned prior from similar challenges",
            "These are reusable patterns from past solves, NOT the answer key. "
            "Adapt them to the current target, do not copy blindly.",
        ]
        for i, tpl in enumerate(templates, 1):
            lines.append(f"\n### Prior #{i}: {tpl.name}")
            lines.append(f"  Category: {tpl.category}")
            if tpl.steps:
                lines.append("  Steps that worked:")
                for step in tpl.steps:
                    lines.append(f"    - {step}")
            if tpl.evidence_chain:
                lines.append("  Evidence chain:")
                for ev in tpl.evidence_chain:
                    lines.append(f"    - {ev}")
        return "\n".join(lines)


def build_retriever(knowledge_root: str | Path | None = None) -> TemplateRetriever:
    """Build a TemplateRetriever from the default or specified knowledge root."""
    if knowledge_root is None:
        knowledge_root = Path(__file__).resolve().parents[3] / "data" / "knowledge"
    return TemplateRetriever(TemplateStore(root=knowledge_root))