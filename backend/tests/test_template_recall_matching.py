"""Template recall must resolve the same keys the distiller wrote.

Two independent defects made the self-produced template store almost unreachable
in production, and both were invisible from a unit test that happened to pass a
``category`` matching the template:

1. ``_template_category`` returned the raw ``challenge_type`` (``WEB_TARGET``)
   while the distiller files every template under the upstream category
   (``web``).  The category term is the heaviest weight in the scorer (+3), so
   recall silently degraded to keyword-only.
2. ``_template_keywords`` tokenised with an ASCII-only regex.  The arena
   catalogue is largely Chinese-titled, so a challenge like
   ``[CISCN 2019 初赛]Love Math`` yielded almost nothing to match on.

Measured on the real store (33 templates): a Chinese-titled challenge went from
1 unrelated hit to 5 hits with its own template first.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.knowledge_context import _template_category, _template_keywords
from app.services.retrieve import TemplateRetriever
from app.solver.muteki.upstream_bridge import (
    category_for_challenge_type,
    upstream_categories,
)
from muteki.learning.distill import KIND_CASE, KIND_NEGATIVE, Template, TemplateStore


def _challenge(**overrides):
    base = {
        "name": "Generic web console",
        "description": "An authorized lab web application.",
        "challenge_type": "WEB_TARGET",
        "target_url": "http://example.test/app",
        "allowed_hosts": ["example.test"],
        "metadata_json": {},
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _template(name: str, kind: str, *, category: str = "web", keywords: list[str] | None = None) -> Template:
    return Template(
        name=name,
        category=category,
        keywords=keywords or ["search", "parameter"],
        steps=["run baseline query"],
        success_format=r"flag\{.*?\}",
        source="challenge-1 (run-1)",
        kind=kind,
    )


# ── category resolution ──────────────────────────────────────────────────────


def test_recall_category_matches_what_the_distiller_stores() -> None:
    """The challenge type maps to the same category the writer files under."""

    assert _template_category(_challenge()) == "web"
    # The value the writer uses for the same challenge type.
    assert category_for_challenge_type("WEB_TARGET") == "web"


@pytest.mark.parametrize(
    ("challenge_type", "expected"),
    [
        ("WEB_TARGET", "web"),
        ("PWN", "pwn"),
        ("REVERSE", "reverse"),
        ("CRYPTO", "crypto"),
        ("FORENSICS", "forensics"),
        ("MISC", "misc"),
        ("web_target", "web"),
        (None, "web"),
        ("SOMETHING_NEW", "web"),
    ],
)
def test_every_challenge_type_resolves_into_the_stored_category_space(
    challenge_type: str | None, expected: str
) -> None:
    resolved = _template_category(_challenge(challenge_type=challenge_type))
    assert resolved == expected
    # The load-bearing invariant: recall can only ever ask for a category that
    # a template may actually have been filed under.
    assert resolved in upstream_categories()


def test_a_raw_challenge_type_carried_as_category_is_still_mapped() -> None:
    """A stale ``category`` holding ``WEB_TARGET`` must not defeat the mapping.

    The challenge model used to expose ``category`` as the raw type; a caller
    still passing that shape has to resolve to ``web`` or the original bug
    returns through the side door.
    """

    assert _template_category(_challenge(category="WEB_TARGET")) == "web"
    # A genuine upstream category is honoured as-is.
    assert _template_category(_challenge(category="crypto")) == "crypto"


# ── keyword tokenisation ─────────────────────────────────────────────────────


def test_ascii_tokens_keep_the_distillers_shape() -> None:
    """English recall must not regress: same shape the distiller stores."""

    tokens = _template_keywords("SQL injection login bypass", "")
    assert "injection" in tokens
    assert "bypass" in tokens
    # Two-letter tokens are below the distiller's threshold and must stay out.
    assert "sql" in tokens


def test_chinese_titles_are_no_longer_invisible() -> None:
    """The arena catalogue is Chinese-titled; those tokens have to survive."""

    tokens = _template_keywords("[CISCN 2019 初赛]Love Math", "PHP 代码审计，绕过字符黑名单")
    assert "初赛" in tokens
    assert "代码审计" in tokens
    # Latin fragments from the same title are still extracted.
    assert "ciscn" in tokens
    assert "love" in tokens


def test_single_han_characters_are_not_treated_as_fingerprints() -> None:
    """One character would match almost every template, so it is excluded."""

    tokens = _template_keywords("题", "")
    assert tokens == []
    assert _template_keywords("密码", "") == ["密码"]


# ── end-to-end recall ────────────────────────────────────────────────────────


def test_a_chinese_titled_challenge_recalls_its_own_template(tmp_path) -> None:
    store = TemplateStore(root=tmp_path)
    store.save(
        _template(
            "[CISCN 2019 初赛]Love Math",
            KIND_NEGATIVE,
            keywords=["ciscn", "love", "math", "初赛"],
        )
    )
    store.save(_template("Unrelated", KIND_CASE, category="pwn", keywords=["totally", "different"]))

    challenge = _challenge(name="[CISCN 2019 初赛]Love Math", description="PHP 代码审计")
    hits = TemplateRetriever(store).retrieve(
        category=_template_category(challenge),
        keywords=_template_keywords(challenge.name, challenge.description),
        top_k=5,
    )
    assert [tpl.name for tpl in hits] == ["[CISCN 2019 初赛]Love Math"]


def test_a_proven_case_outranks_a_negative_on_a_tie(tmp_path) -> None:
    """``this worked`` is a stronger prior than ``do not repeat this``.

    With 17 negatives against 16 positives, a plain score sort let negatives
    crowd the positive block out of the ``top_k`` window.
    """

    store = TemplateStore(root=tmp_path)
    store.save(_template("zzz-negative", KIND_NEGATIVE, keywords=["shared"]))
    store.save(_template("aaa-case", KIND_CASE, keywords=["shared"]))

    hits = TemplateRetriever(store).retrieve(category="web", keywords=["shared"], top_k=1)
    assert [tpl.kind for tpl in hits] == [KIND_CASE]


def test_the_category_term_alone_cannot_recall_a_template(tmp_path) -> None:
    """Category is a *ranking* bonus, never a reason to be recalled.

    The corpus is almost entirely ``web``, so a category-only match means
    "another web challenge" -- an arbitrary neighbour.  Showing one is how every
    arena challenge ended up being handed ``[PHPMYADMIN]CVE-2016-5734``'s
    ``root/root`` login recipe on 2026-10-22.  Empty recall is the correct
    answer here; the caller already renders nothing for an empty block.
    """

    store = TemplateStore(root=tmp_path)
    store.save(_template("only-category", KIND_CASE, keywords=["nothing", "overlapping"]))

    hits = TemplateRetriever(store).retrieve(category="web", keywords=["absent"], top_k=3)
    assert hits == []


def test_shared_url_fragments_cannot_make_every_template_a_match(tmp_path) -> None:
    """Every arena brief links a vulhub URL, so those tokens are worthless.

    ``cve``/``github``/``vulhub``/``blob``/``master`` appear in nearly every
    template.  Counting them gave every candidate the same score, so the sort
    fell through to the name and one template won every query.
    """

    store = TemplateStore(root=tmp_path)
    store.save(
        _template(
            "phpmyadmin-case",
            KIND_CASE,
            keywords=["phpmyadmin", "cve", "github", "vulhub", "blob", "master"],
        )
    )
    store.save(
        _template(
            "struts2-case",
            KIND_CASE,
            keywords=["struts2", "cve", "github", "vulhub", "blob", "master"],
        )
    )
    retriever = TemplateRetriever(store)

    struts = retriever.retrieve(
        category="web",
        keywords=["cve", "github", "vulhub", "blob", "master", "struts2"],
        top_k=2,
    )
    assert [tpl.name for tpl in struts] == ["struts2-case"]

    # Only URL fragments: nothing distinctive was said, so nothing is recalled.
    fragments_only = retriever.retrieve(
        category="web",
        keywords=["cve", "github", "vulhub", "blob", "master"],
        top_k=2,
    )
    assert fragments_only == []


def test_the_real_store_recalls_a_chinese_titled_challenge() -> None:
    """Regression against the shipped templates, not a synthetic store.

    This is the case that actually failed in production: a Chinese-titled arena
    challenge recalled one unrelated template before the fix.
    """

    root = Path(__file__).resolve().parents[2] / "data" / "knowledge" / "templates"
    if not root.is_dir() or not any(root.glob("*.yaml")):
        pytest.skip("shipped template store is not present in this checkout")

    challenge = _challenge(name="[CISCN 2019 初赛]Love Math", description="PHP 代码审计")
    hits = TemplateRetriever(TemplateStore(root=root)).retrieve(
        category=_template_category(challenge),
        keywords=_template_keywords(challenge.name, challenge.description),
        top_k=5,
    )
    assert hits, "a Chinese-titled challenge must recall something"
    assert hits[0].name == "[CISCN 2019 初赛]Love Math"


# ── observability: recall must be answerable from the event log alone ────────


def test_learned_recall_reports_which_templates_it_returned() -> None:
    """The self-produced channel used to be recorded as a bare character count.

    ``run_events`` stored only the curated ``knowledge_item_ids``, so "did the
    learned prior reach the prompt, and which template was it?" could only be
    answered by reproducing the call by hand -- which is how the every-challenge
    recall failure below went unnoticed for so long.
    """

    from app.services.knowledge_context import _learned_prior_detail

    block, names = _learned_prior_detail(
        category="web",
        keywords=["struts2", "s2-046", "ognl"],
        top_k=2,
        max_chars=600,
    )
    assert names, "the shipped store has a struts2 template; recall must name it"
    assert block
    for name in names:
        assert name in block or name.split("]")[-1] in block


def test_learned_recall_returns_no_names_when_it_returns_no_block() -> None:
    """Empty recall must be reported as empty, not as "recall never ran"."""

    from app.services.knowledge_context import _learned_prior_detail

    block, names = _learned_prior_detail(
        category="web",
        keywords=["zzz-no-such-stack"],
        top_k=2,
        max_chars=600,
    )
    assert block == ""
    assert names == []


def test_a_challenge_does_not_recall_a_different_stacks_template() -> None:
    """The failure this guards: every challenge recalled one phpMyAdmin template.

    Every arena brief is a vulhub URL, so ``cve``/``github``/``vulhub``/
    ``blob``/``master`` appear in nearly every template.  Scoring them made all
    candidates tie, the name broke the tie, and one template won every query.
    """

    from app.services.knowledge_context import _learned_prior_detail

    block, names = _learned_prior_detail(
        category="web",
        keywords=["struts2", "s2-046", "ognl", "cve", "github", "vulhub", "blob", "master"],
        top_k=2,
        max_chars=600,
    )
    assert names
    assert not any("PHPMYADMIN" in name.upper() for name in names), names
