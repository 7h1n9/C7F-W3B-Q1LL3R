"""Worker-scoped knowledge retrieval.

The Coordinator Reason pass already receives a challenge-scoped knowledge prior.
Workers previously received none, so a Worker could only act on whatever
technique hint Reason happened to write into its intent.  These tests pin the
intent-scoped retrieval contract: relevance follows the Worker's own direction,
the block stays inside a bounded budget, and the off-path still imports no
retriever.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services import knowledge_context
from app.services.knowledge_context import KnowledgeContextService


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


def test_worker_prior_is_disabled_by_run_hint_without_touching_config(monkeypatch) -> None:
    def forbidden_config():
        raise AssertionError("off mode must not load knowledge config")

    monkeypatch.setattr(knowledge_context, "load_knowledge_config", forbidden_config)

    result = KnowledgeContextService.build_worker_prior(
        SimpleNamespace(hints_json={"knowledge_mode": "off"}),
        _challenge(),
        intent_goal="Probe the search parameter for SQL injection",
    )

    assert result == {"mode": "off", "enabled": False, "prior": "", "reason": "disabled_by_run"}


def test_worker_prior_follows_the_intent_not_only_the_challenge(monkeypatch) -> None:
    monkeypatch.setenv("C7F_KNOWLEDGE_MODE", "off")
    monkeypatch.delenv("C7F_KNOWLEDGE_ROOT", raising=False)
    knowledge_context.clear_retriever_cache()

    result = KnowledgeContextService.build_worker_prior(
        SimpleNamespace(hints_json={"knowledge_mode": "on"}),
        _challenge(),
        intent_goal="Confirm path traversal and read /etc/passwd via the preview parameter",
        tool_name="http_request",
    )

    assert result["mode"] == "on"
    assert result["enabled"] is True
    assert "path-traversal-file-read" in result["item_ids"]
    assert "待验证先验" in result["prior"]
    assert "NOT a VerifiedFact" in result["prior"]
    assert len(result["prior"]) <= knowledge_context._MAX_WORKER_PRIOR_CHARS


def test_worker_prior_ranks_the_intent_category_above_a_generic_challenge(monkeypatch) -> None:
    monkeypatch.setenv("C7F_KNOWLEDGE_MODE", "on")
    monkeypatch.delenv("C7F_KNOWLEDGE_ROOT", raising=False)
    knowledge_context.clear_retriever_cache()

    ssti = KnowledgeContextService.build_worker_prior(
        SimpleNamespace(hints_json={"knowledge_mode": "on"}),
        _challenge(description="A templating playground for a web console."),
        intent_goal="Test the 模板 render endpoint with Jinja2 server-side template injection payloads",
    )
    command = KnowledgeContextService.build_worker_prior(
        SimpleNamespace(hints_json={"knowledge_mode": "on"}),
        _challenge(description="A network 连通性诊断 console."),
        intent_goal="Test the ping diagnostic parameter for OS command injection",
    )

    assert ssti["item_ids"][0] == "ssti-jinja2-detection"
    assert command["item_ids"][0] == "command-injection-parameter"


def test_worker_prior_bounds_length_and_rejects_empty_query(monkeypatch) -> None:
    monkeypatch.setenv("C7F_KNOWLEDGE_MODE", "on")
    monkeypatch.delenv("C7F_KNOWLEDGE_ROOT", raising=False)
    knowledge_context.clear_retriever_cache()

    bounded = KnowledgeContextService.build_worker_prior(
        SimpleNamespace(hints_json={"knowledge_mode": "on"}),
        _challenge(),
        intent_goal="SQL injection and command injection and path traversal and IDOR and SSRF probes",
        max_chars=400,
    )
    assert bounded["enabled"] is True
    assert len(bounded["prior"]) <= 400

    empty = KnowledgeContextService.build_worker_prior(
        SimpleNamespace(hints_json={"knowledge_mode": "on"}),
        SimpleNamespace(),
        intent_goal="   ",
    )
    assert empty["enabled"] is False
    assert empty["reason"] == "empty_worker_query"


def test_retriever_cache_reuses_real_indexes_but_not_test_doubles(monkeypatch) -> None:
    calls: list[object] = []

    class FakeRetriever:
        def __init__(self, config):
            calls.append(config)

    monkeypatch.setattr(knowledge_context, "create_retriever", lambda config: FakeRetriever(config))
    knowledge_context.clear_retriever_cache()
    config = SimpleNamespace(root="corpus", top_k=3, mode="on")

    assert knowledge_context._cached_retriever(config) is not knowledge_context._cached_retriever(config)
    assert len(calls) == 2
