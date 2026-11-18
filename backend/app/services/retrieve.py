"""Template retrieval (§16) — recall distilled templates for similar challenges.

The official Muteki ``learning/retrieve.py`` matches the current challenge's
category and keywords against stored templates and returns the best matches as
a PRIOR for the solver, not an answer key.

These templates are produced by this project's own finished Runs, so they are
deliberately labelled a *weaker* evidence class than the human-curated JSONL
corpus handled by :mod:`app.knowledge.retriever`.  Two kinds are stored:

``case``
    A route that actually captured a flag.  Reusable as a positive pattern.
``negative``
    A route the swarm tried and ruled out.  Reusable as an explicit "do not
    repeat this".
"""

from __future__ import annotations

from pathlib import Path

from muteki.learning.distill import KIND_CASE, KIND_NEGATIVE, Template, TemplateStore

DEFAULT_LEARNED_PRIOR_CHARS = 900


def _compact_line(value: str, limit: int = 200) -> str:
    compacted = " ".join(str(value or "").split())
    if len(compacted) <= limit:
        return compacted
    return compacted[: limit - 3].rstrip() + "..."


#: Tokens that carry no discriminative power between templates.
#:
#: Every arena brief is a ``github.com/vulhub/vulhub/blob/master/<stack>/<cve>``
#: URL, and the distiller tokenises that URL verbatim, so ``cve``, ``github``,
#: ``vulhub``, ``blob`` and ``master`` land in almost every template's keyword
#: list.  Counting them made every candidate score identically (category +3 and
#: five shared words = 8), which left the name as the only tiebreak: measured on
#: 2026-10-22, *every* challenge -- struts2, ThinkPHP, Spring -- recalled
#: ``[PHPMYADMIN]CVE-2016-5734`` and was handed the ``root/root`` phpMyAdmin
#: login recipe.  A wrong prior is worse than no prior, so these tokens are
#: scored as if they were not there.
_GENERIC_TEMPLATE_TOKENS = frozenset(
    {
        "cve",
        "cves",
        "github",
        "vulhub",
        "blob",
        "master",
        "http",
        "https",
        "com",
        "www",
        "raw",
        "tree",
        "main",
        "refs",
        "heads",
        "readme",
        "index",
        "html",
    }
)


class TemplateRetriever:
    """Lightweight keyword-match retrieval over the TemplateStore."""

    def __init__(self, store: TemplateStore) -> None:
        self.store = store

    def retrieve(
        self,
        *,
        category: str,
        keywords: list[str],
        top_k: int = 3,
        kinds: set[str] | None = None,
    ) -> list[Template]:
        """Return the ``top_k`` templates most relevant to the given challenge.

        Scoring:
          - exact category match: +3
          - each overlapping *distinctive* keyword: +1

        A template with no distinctive overlap is dropped even when its category
        matches.  Category alone means "another web challenge", which is not a
        reason to show its steps: the corpus is almost entirely ``web``, so a
        category-only match would surface an arbitrary neighbour.  Recall is
        allowed to return nothing -- the caller already treats an empty block as
        "no prior".

        ``kinds`` optionally restricts the search to specific template kinds so
        a caller can ask for positives and negatives separately.
        """
        templates = self.store.load_all()
        scored: list[tuple[int, int, str, Template]] = []

        for tpl in templates:
            if kinds is not None and tpl.kind not in kinds:
                continue
            distinctive = [
                kw
                for kw in keywords
                if kw not in _GENERIC_TEMPLATE_TOKENS and kw in tpl.keywords
            ]
            if not distinctive:
                continue
            score = len(distinctive)
            if tpl.category == category:
                score += 3
            if score > 0:
                # ``case`` before ``negative`` on a tie.  Both are useful, but a
                # proven success path ("this worked") is a stronger prior than a
                # ruled-out route ("do not repeat this"), and with 17 negatives
                # against 16 positives a plain score sort let negatives crowd the
                # positive block out of the ``top_k`` window entirely -- so the
                # solver could lose the one thing that actually solved the
                # challenge.  The name is the final tiebreak so the order is
                # stable rather than dependent on directory listing order.
                is_case = 0 if tpl.kind == KIND_CASE else 1
                scored.append((score, is_case, tpl.name, tpl))

        scored.sort(key=lambda x: (-x[0], x[1], x[2]))
        return [tpl for _, _, _, tpl in scored[:top_k]]

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


def format_learned_prior(
    templates: list[Template],
    *,
    max_chars: int = DEFAULT_LEARNED_PRIOR_CHARS,
) -> str:
    """Format distilled templates as a two-part, explicitly low-trust PRIOR block.

    Positives and negatives are kept visually separate on purpose: "this worked
    before" and "do not repeat this" are opposite instructions, and merging them
    into one list is how a prior starts misdirecting the solver.
    """
    positives = [tpl for tpl in templates if tpl.kind == KIND_CASE]
    negatives = [tpl for tpl in templates if tpl.kind == KIND_NEGATIVE]
    if not positives and not negatives:
        return ""

    lines = [
        "Learned prior from THIS project's own finished runs "
        "(自产轨迹，可信级别低于人工整理语料):",
    ]
    if positives:
        lines.append("Patterns that worked before (from flag-verified runs):")
        for tpl in positives:
            lines.append(f"- {tpl.category}/{tpl.name}")
            for step in tpl.steps[:3]:
                lines.append(f"    * {_compact_line(step)}")
    if negatives:
        lines.append("Routes already ruled out by past runs (do NOT repeat blindly):")
        for tpl in negatives:
            avoided = " | ".join(_compact_line(step, 120) for step in tpl.steps[:3])
            lines.append(f"- {tpl.category}/{tpl.name}: {avoided}")

    rendered = "\n".join(lines)
    if len(rendered) <= max_chars:
        return rendered
    suffix = "\n[learned prior truncated]"
    return rendered[: max(max_chars - len(suffix), 0)].rstrip() + suffix


def build_retriever(knowledge_root: str | Path | None = None) -> TemplateRetriever:
    """Build a TemplateRetriever from the default or specified knowledge root."""
    if knowledge_root is None:
        knowledge_root = Path(__file__).resolve().parents[3] / "data" / "knowledge"
    return TemplateRetriever(TemplateStore(root=knowledge_root))
