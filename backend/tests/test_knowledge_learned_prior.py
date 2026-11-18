"""The self-generated knowledge loop: distill to store, then recall in a Worker.

Before this loop existed the two ends were unconnected: distillation wrote to a
directory nobody read, ``SharedGraph`` (a typing Protocol) could not even be
instantiated, and ``TemplateRetriever`` had no caller anywhere in the repo.  The
loop is now: a finished Run is distilled into ``<knowledge root>/templates``,
and the next Worker turn recalls it as an explicitly low-trust PRIOR.

Rejection sampling is the rule that makes the loop safe: only a flag-verified
Run may produce a ``case`` template; a Run that ended without a flag produces a
``negative`` recording the ruled-out routes.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.knowledge.config import knowledge_templates_root
from app.services import knowledge_context
from app.services.knowledge_context import KnowledgeContextService
from app.services.retrieve import TemplateRetriever, format_learned_prior
from muteki.learning.distill import (
    KIND_CASE,
    KIND_NEGATIVE,
    Template,
    TemplateStore,
)


def _challenge(**overrides):
    base = {
        "name": "Generic web console",
        "description": "An authorized lab web application.",
        "challenge_type": "WEB_TARGET",
        "category": "WEB_TARGET",
        "target_url": "http://example.test/app",
        "allowed_hosts": ["example.test"],
        "metadata_json": {},
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _template(name: str, kind: str, *, steps: list[str] | None = None) -> Template:
    return Template(
        name=name,
        category="WEB_TARGET",
        keywords=["search", "parameter", "sql"],
        steps=steps or ["run baseline query", "flip the boolean predicate"],
        success_format=r"flag\{.*?\}",
        source="challenge-1 (run-1)",
        kind=kind,
    )


def _point_templates_at(monkeypatch, tmp_path) -> TemplateStore:
    root = tmp_path / "knowledge" / "templates"
    monkeypatch.setenv("C7F_KNOWLEDGE_TEMPLATES_ROOT", str(root))
    return TemplateStore(root=root)


def test_templates_root_sits_beside_the_corpus(monkeypatch) -> None:
    monkeypatch.delenv("C7F_KNOWLEDGE_TEMPLATES_ROOT", raising=False)
    monkeypatch.delenv("C7F_KNOWLEDGE_ROOT", raising=False)
    default_root = knowledge_templates_root()
    assert default_root.name == "templates"
    assert default_root.parent.name == "knowledge"

    monkeypatch.setenv("C7F_KNOWLEDGE_TEMPLATES_ROOT", "/tmp/elsewhere")
    assert knowledge_templates_root() == Path("/tmp/elsewhere")


def test_case_templates_stay_yaml_compatible_and_negatives_are_labelled(tmp_path) -> None:
    store = TemplateStore(root=tmp_path)

    case_path = store.save(_template("solved-login", KIND_CASE))
    # The new field must not perturb existing case templates on disk.
    assert "kind:" not in case_path.read_text(encoding="utf-8")

    negative_path = store.save(_template("timed-out-login", KIND_NEGATIVE))
    assert "kind: negative" in negative_path.read_text(encoding="utf-8")

    # A negative for the same challenge name must never clobber the positive.
    assert case_path != negative_path
    reloaded = {tpl.name: tpl.kind for tpl in store.load_all()}
    assert reloaded == {"solved-login": KIND_CASE, "timed-out-login": KIND_NEGATIVE}


def test_learned_prior_separates_worked_from_ruled_out() -> None:
    rendered = format_learned_prior(
        [
            _template("solved-login", KIND_CASE),
            _template("timed-out-login", KIND_NEGATIVE, steps=["LDAP bind injection returned 500"]),
        ]
    )
    assert "自产轨迹" in rendered
    assert "可信级别低于" in rendered
    assert "Patterns that worked before" in rendered
    assert "Routes already ruled out" in rendered
    assert "LDAP bind injection" in rendered
    assert rendered.index("Patterns that worked before") < rendered.index(
        "Routes already ruled out"
    )


def test_learned_prior_is_empty_without_templates() -> None:
    assert format_learned_prior([]) == ""


def test_retriever_can_filter_by_kind(tmp_path) -> None:
    store = TemplateStore(root=tmp_path)
    store.save(_template("solved-login", KIND_CASE))
    store.save(_template("timed-out-login", KIND_NEGATIVE))
    retriever = TemplateRetriever(store)

    negatives = retriever.retrieve(
        category="WEB_TARGET", keywords=["search"], top_k=5, kinds={KIND_NEGATIVE}
    )
    assert [tpl.name for tpl in negatives] == ["timed-out-login"]


def test_worker_prior_recalls_a_distilled_template(monkeypatch, tmp_path) -> None:
    store = _point_templates_at(monkeypatch, tmp_path)
    store.save(_template("solved-login", KIND_CASE))
    store.save(
        _template("timed-out-login", KIND_NEGATIVE, steps=["LDAP bind injection returned 500"])
    )
    monkeypatch.setenv("C7F_KNOWLEDGE_MODE", "on")
    monkeypatch.delenv("C7F_KNOWLEDGE_ROOT", raising=False)
    knowledge_context.clear_retriever_cache()

    result = KnowledgeContextService.build_worker_prior(
        SimpleNamespace(hints_json={"knowledge_mode": "on"}),
        _challenge(),
        intent_goal="Probe the search parameter for SQL injection",
        tool_name="http_request",
    )

    assert result["enabled"] is True
    assert result["learned_prior"]
    assert "solved-login" in result["learned_prior"]
    assert "timed-out-login" in result["learned_prior"]
    # The learned block is appended to the curated block, and together they stay
    # inside the Worker turn's budget.
    assert result["learned_prior"] in result["prior"]
    assert len(result["prior"]) <= knowledge_context._MAX_WORKER_PRIOR_CHARS
    # Curated content keeps the majority of the budget.
    assert len(result["learned_prior"]) <= knowledge_context._MAX_LEARNED_PRIOR_CHARS


def test_worker_prior_without_templates_is_unchanged(monkeypatch, tmp_path) -> None:
    """An empty/absent template store must not shrink the curated block."""

    monkeypatch.setenv("C7F_KNOWLEDGE_TEMPLATES_ROOT", str(tmp_path / "missing"))
    monkeypatch.setenv("C7F_KNOWLEDGE_MODE", "on")
    monkeypatch.delenv("C7F_KNOWLEDGE_ROOT", raising=False)
    knowledge_context.clear_retriever_cache()

    result = KnowledgeContextService.build_worker_prior(
        SimpleNamespace(hints_json={"knowledge_mode": "on"}),
        _challenge(),
        intent_goal="Probe the search parameter for SQL injection",
    )

    assert result["enabled"] is True
    assert result["learned_prior"] == ""
    assert "待验证先验" in result["prior"]


def test_learned_recall_failure_never_blocks_the_prior(monkeypatch, tmp_path) -> None:
    _point_templates_at(monkeypatch, tmp_path)
    monkeypatch.setenv("C7F_KNOWLEDGE_MODE", "on")
    monkeypatch.delenv("C7F_KNOWLEDGE_ROOT", raising=False)
    knowledge_context.clear_retriever_cache()

    def explode(*args, **kwargs):
        raise RuntimeError("template store unavailable")

    monkeypatch.setattr("app.services.retrieve.TemplateRetriever", explode)
    (tmp_path / "knowledge" / "templates").mkdir(parents=True, exist_ok=True)
    (tmp_path / "knowledge" / "templates" / "web__x.yaml").write_text(
        "name: x\ncategory: WEB_TARGET\n", encoding="utf-8"
    )

    result = KnowledgeContextService.build_worker_prior(
        SimpleNamespace(hints_json={"knowledge_mode": "on"}),
        _challenge(),
        intent_goal="Probe the search parameter for SQL injection",
    )

    assert result["learned_prior"] == ""
    assert result["enabled"] is True
    assert "待验证先验" in result["prior"]
