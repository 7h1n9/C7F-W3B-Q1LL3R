import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.services.knowledge_context as knowledge_context
from app.knowledge.config import (
    KnowledgeConfig,
    load_knowledge_config,
    resolve_knowledge_mode,
)
from app.knowledge.loader import KnowledgeLoader, load_knowledge_items
from app.knowledge.models import (
    KnowledgeItem,
    KnowledgeProvenance,
    RetrievalRecord,
    RetrievalResult,
)
from app.knowledge.retriever import KnowledgeRetriever, cve_ids, format_prior
from app.services.knowledge_context import KnowledgeContextService


def _item(
    item_id: str,
    *,
    title: str,
    technique: str,
    summary: str,
    kind: str = "technique",
    target_stack: list[str] | None = None,
) -> KnowledgeItem:
    return KnowledgeItem(
        id=item_id,
        kind=kind,
        title=title,
        technique=technique,
        summary=summary,
        preconditions=["Target is in scope."],
        target_stack=target_stack or ["web"],
        payload_template="' OR 1=1--",
        verification_method=["Compare a control request with the candidate request."],
        known_failures=["WAF may normalize or block the payload."],
        steps=["Send a control request.", "Send the candidate request.", "Compare results."],
        provenance=KnowledgeProvenance(
            source_type="standard",
            source_ref="OWASP test reference",
        ),
        published_at=date(2024, 1, 1),
        trust_level="curated",
    )


def _payload(item_id: str) -> dict:
    return _item(
        item_id,
        title=f"Title {item_id}",
        technique="sql-injection",
        summary="SQL injection test procedure.",
    ).model_dump(mode="json")


def _retriever(items: list[KnowledgeItem], *, mode: str = "on") -> KnowledgeRetriever:
    return KnowledgeRetriever(
        items=items,
        config=KnowledgeConfig(mode=mode, root=Path("."), top_k=3),
    )


def test_a_named_cve_binds_to_its_own_entry_and_excludes_its_siblings():
    """A neighbouring CVE is not a weaker match -- it is a different bug.

    Measured on 2026-10-20: the corpus had no entry for CVE-2017-10271, so
    retrieval substituted the nearest neighbour (a WebLogic SSRF writeup, score
    62.56 on "weblogic" plus repository boilerplate) and the Worker spent the
    whole round on the console path of CVE-2020-14882.  The CVE id is the one
    identifier that is exact, so it is matched literally and a sibling CVE is
    dropped rather than ranked second.
    """

    correct = _item(
        "cve-10271-wsat",
        title="WebLogic WLS-WSAT XMLDecoder RCE",
        technique="XML decoder deserialization CVE-2017-10271",
        summary="POST the XMLDecoder payload to /wsl-wsat/CoordinatorPortType.",
        target_stack=["weblogic"],
    )
    sibling = _item(
        "cve-14882-console",
        title="WebLogic console path traversal",
        technique="console bypass CVE-2020-14882",
        summary="GET /console/css/%252e%252e%252fconsole.portal.",
        target_stack=["weblogic"],
    )
    retriever = _retriever([sibling, correct])

    results = retriever.retrieve(
        query=(
            "Challenge: [Weblogic]CVE-2017-10271\n"
            "Description: https://github.com/vulhub/vulhub/tree/master/weblogic/CVE-2017-10271"
        ),
        metadata={"challenge_type": "WEB_TARGET"},
        top_k=3,
    )

    # The sibling shares the product, the port and the corpus vocabulary -- and
    # is still not this vulnerability.
    assert [result.item.id for result in results] == ["cve-10271-wsat"]


def test_repository_boilerplate_alone_is_not_relevance():
    """``github.com/vulhub/tree/master`` rides along in every vulhub description.

    Those tokens are what let an unrelated writeup outrank an entry that
    actually describes the vulnerability, so on their own they must not put an
    item in the result set.
    """

    readme_only = _item(
        "readme-only",
        title="Lecture notes",
        technique="misc",
        summary="See the github.com/vulhub readme for the lab setup.",
        target_stack=["web"],
    )
    retriever = _retriever([readme_only])

    results = retriever.retrieve(
        query="Description: https://github.com/vulhub/vulhub/tree/master/target",
        metadata={},
        top_k=3,
    )

    assert results == []


def test_cve_ids_are_normalised_for_literal_matching():
    assert cve_ids("Challenge: [Weblogic]CVE-2017-10271") == {"cve-2017-10271"}
    assert cve_ids("cve_2018_2894 and CVE 2020 14882") == {"cve-2018-2894", "cve-2020-14882"}
    assert cve_ids("no identifier here") == set()


def test_playbook_paths_match_the_real_endpoints():
    """The path in the CVE playbook is copied straight into a Worker prompt.

    ``wsl-wsat`` versus ``wls-wsat`` is one transposed letter and the wrong one
    is a 404.  Measured on 2026-10-20: the playbook shipped the typo in all
    seven places, and the only reason the round still worked is that the Worker
    reached ``/wls-wsat/CoordinatorPortType`` (HTTP 200) from its own knowledge
    rather than from the prior.
    """

    items = {item.id: item for item in load_knowledge_items()}
    wsat = items.get("cve-2017-10271-weblogic-ws-at")
    assert wsat is not None, "the 10271 playbook entry must stay in the corpus"
    blob = " ".join(
        [wsat.summary, wsat.payload_template or "", *(wsat.preconditions or []), *(wsat.steps or [])]
    )
    assert "/wls-wsat/CoordinatorPortType" in blob, blob[:200]
    assert "wsl-wsat" not in blob


def test_curated_corpus_loads_with_schema_dates_and_unique_ids():
    items = load_knowledge_items()
    ids = [item.id for item in items]

    # The hand-curated corpus is the items shipped in the project's own JSONL
    # files.  The reference-corpus import adds more files beside them, so this
    # asserts the curated floor rather than a total: growing the corpus must
    # never be able to silently drop one of the reviewed entries.  A floor, not
    # an equality, because a hand-verified entry legitimately joins this group
    # (CVE-2018-12613 was promoted to ``reviewed`` after being solved end to end
    # on the arena); the invariant worth pinning is "no reviewed entry vanished".
    curated = [item for item in items if item.provenance.source_type != "reference-corpus"]
    assert len(curated) >= 31
    assert len(items) >= len(curated)
    assert len(ids) == len(set(ids))
    assert all(item.published_at is None or item.published_at < date(2025, 1, 1) for item in items)
    assert all(item.provenance.source_ref.strip() for item in items)
    assert all(item.steps and item.verification_method for item in items)
    # ``case`` entries assert something about a whole route, so the loader holds
    # them to the same ``known_failures`` requirement as ``negative`` entries.
    assert all(item.known_failures for item in items if item.kind in {"case", "negative"})


def test_loader_rejects_invalid_json_schema_and_duplicate_ids(tmp_path):
    malformed = tmp_path / "malformed.jsonl"
    malformed.write_text('{"id": "broken"', encoding="utf-8")
    with pytest.raises(ValueError, match="invalid JSON"):
        KnowledgeLoader(malformed).load()

    invalid_schema = tmp_path / "invalid-schema.jsonl"
    payload = _payload("valid-id")
    payload.pop("provenance")
    invalid_schema.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid knowledge item"):
        KnowledgeLoader(invalid_schema).load()

    duplicate = tmp_path / "duplicate.jsonl"
    duplicate.write_text(
        json.dumps(_payload("same-id")) + "\n" + json.dumps(_payload("same-id")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate knowledge id"):
        KnowledgeLoader(duplicate).load()


def test_loader_requires_known_failures_for_route_level_kinds(tmp_path):
    """``case`` and ``negative`` describe a route, so both need their limits."""

    def write(name: str, **overrides) -> Path:
        payload = _item(
            name,
            title="Route level entry",
            technique="strategy",
            summary="A route-level strategy summary.",
        ).model_dump(mode="json")
        payload.update(overrides)
        path = tmp_path / f"{name}.jsonl"
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        return path

    with pytest.raises(ValueError, match="case knowledge requires known_failures"):
        KnowledgeLoader(write("case-no-failures", kind="case", known_failures=[])).load()

    with pytest.raises(ValueError, match="negative knowledge requires known_failures"):
        KnowledgeLoader(write("negative-no-failures", kind="negative", known_failures=[])).load()

    # A technique may still omit them: the requirement is about route-level claims.
    KnowledgeLoader(write("technique-no-failures", kind="technique", known_failures=[])).load()

    accepted = KnowledgeLoader(
        write("case-with-failures", kind="case", known_failures=["Fails behind a WAF."])
    ).load()
    assert [item.kind for item in accepted] == ["case"]


def test_config_and_run_hints_resolve_mode_without_mutating_environment(monkeypatch):
    monkeypatch.setenv("C7F_KNOWLEDGE_MODE", "on")
    monkeypatch.setenv("C7F_KNOWLEDGE_TOP_K", "4")

    config = load_knowledge_config()

    assert config.mode == "on"
    assert config.top_k == 4
    assert resolve_knowledge_mode({"knowledge_mode": "off"}, "on") == "off"
    assert resolve_knowledge_mode({}, "on") == "on"


def test_bm25_metadata_top_k_and_stable_tie_break():
    items = [
        _item(
            "item-b",
            title="Boolean SQL injection",
            technique="sql-injection",
            summary="Boolean sql injection sql injection oracle.",
        ),
        _item(
            "item-a",
            title="Boolean SQL injection",
            technique="sql-injection",
            summary="Boolean sql injection sql injection oracle.",
        ),
        _item(
            "poc-only",
            title="POC for SQL injection",
            technique="sql-injection",
            summary="A proof of concept for SQL injection.",
            kind="poc",
        ),
        _item(
            "xss",
            title="Reflected XSS",
            technique="xss",
            summary="Reflect script context.",
        ),
    ]
    retriever = _retriever(items)

    tied = retriever.retrieve(query="sql injection", top_k=2)
    assert [result.item.id for result in tied] == ["item-a", "item-b"]
    assert all(result.score > 0 for result in tied)

    filtered = retriever.retrieve(
        query="sql injection",
        metadata={"kind": "poc", "technique": "sql-injection"},
        top_k=3,
    )
    assert [result.item.id for result in filtered] == ["poc-only"]


def test_format_prior_and_retrieval_log_are_explicit_and_stable():
    item = _item(
        "sqli-prior",
        title="SQL injection boolean oracle",
        technique="sql-injection",
        summary="Compare true and false predicates.",
    )
    retriever = _retriever([item])

    results = retriever.retrieve(
        query="sql injection boolean",
        metadata={"challenge_type": "sql-injection"},
        top_k=1,
    )
    rendered = retriever.format_prior(results)
    log = retriever.last_log()

    assert results[0].item.id == "sqli-prior"
    assert "待验证先验" in rendered
    assert "VerifiedFact" in rendered
    assert "Use these items only to propose hypotheses" in rendered
    assert "sql-injection" in rendered
    assert isinstance(log, RetrievalRecord)
    assert log.query == "sql injection boolean"
    assert log.metadata == {"challenge_type": "sql-injection"}
    assert log.top_k == 1
    assert [result.item.id for result in log.results] == ["sqli-prior"]
    assert format_prior([]).startswith("Knowledge prior: no relevant items found")


def test_retriever_off_mode_returns_empty_and_rejects_invalid_top_k():
    retriever = _retriever(
        [
            _item(
                "sqli-prior",
                title="SQL injection",
                technique="sql-injection",
                summary="SQL injection.",
            )
        ],
        mode="off",
    )

    assert retriever.retrieve(query="sql injection") == []
    assert retriever.last_log() is not None
    assert retriever.last_log().results == []
    with pytest.raises(ValueError, match="top_k"):
        retriever.retrieve(query="sql injection", top_k=0)


def test_context_service_off_does_not_load_config_or_retriever(monkeypatch):
    called: list[str] = []

    def forbidden_config():
        called.append("config")
        raise AssertionError("off mode must not load config")

    def forbidden_retriever(_config):
        called.append("retriever")
        raise AssertionError("off mode must not create a retriever")

    monkeypatch.setattr(knowledge_context, "load_knowledge_config", forbidden_config)
    monkeypatch.setattr(knowledge_context, "create_retriever", forbidden_retriever)

    result = KnowledgeContextService.build(
        SimpleNamespace(hints_json={"knowledge_mode": "off"}),
        SimpleNamespace(),
    )

    assert result == {"mode": "off", "enabled": False, "reason": "disabled_by_run"}
    assert called == []


def test_context_service_accepts_pseudo_run_without_hints_json(monkeypatch):
    config = SimpleNamespace(mode="off", top_k=3, disabled_reason="disabled_by_environment")
    monkeypatch.setattr(knowledge_context, "load_knowledge_config", lambda: config)

    result = KnowledgeContextService.build(SimpleNamespace(), SimpleNamespace())

    assert result == {
        "mode": "off",
        "enabled": False,
        "reason": "disabled_by_environment",
    }


def test_context_service_on_retrieves_once_and_never_promotes_prior(monkeypatch):
    item = _item(
        "sqli-prior",
        title="Boolean SQL injection",
        technique="sql-injection",
        summary="Use a boolean oracle after observing a SQL error.",
    )
    retrieval_result = RetrievalResult(
        item=item,
        score=1.25,
        matched_terms=["sql", "injection"],
    )
    calls: dict[str, object] = {}

    class FakeRetriever:
        def retrieve(self, *, query, metadata, top_k):
            calls["retrieve"] = {
                "query": query,
                "metadata": metadata,
                "top_k": top_k,
            }
            from datetime import datetime

            calls["log"] = RetrievalRecord(
                query=query,
                metadata=metadata,
                top_k=top_k,
                results=[retrieval_result],
                searched_at=datetime(2024, 1, 1),
            )
            return [retrieval_result]

        def format_prior(self, results):
            assert results == [retrieval_result]
            return "Knowledge prior (待验证先验):\n[sqli-prior] Boolean SQL injection"

        def last_log(self):
            return calls["log"]

    config = SimpleNamespace(mode="on", top_k=2, disabled_reason=None)
    monkeypatch.setattr(knowledge_context, "load_knowledge_config", lambda: config)
    monkeypatch.setattr(
        knowledge_context,
        "create_retriever",
        lambda resolved: FakeRetriever(),
    )
    challenge = SimpleNamespace(
        name="SQL challenge",
        description="Probe the login search field.",
        challenge_type="WEB_TARGET",
        target_url="http://example.test:8080/login",
        allowed_hosts=["example.test"],
        metadata_json={
            "target_stack": ["python", "sqlite"],
            "ground_truth": "flag{do-not-leak}",
            "solution": "Use the secret answer.",
        },
    )
    state = SimpleNamespace(confirmed_facts_json=[], capability_ledger_json={})
    solver_state = SimpleNamespace(
        current_phase="RECON",
        confirmed_facts=[],
        ranked_hypotheses=[
            {
                "statement": "The search parameter may be injectable.",
                "category": "SQLi",
                "status": "OPEN",
                "confidence": 0.8,
            }
        ],
    )

    result = KnowledgeContextService.build(
        SimpleNamespace(hints_json={"knowledge_mode": "on"}),
        challenge,
        state=state,
        solver_state=solver_state,
    )

    assert result["mode"] == "on"
    assert result["enabled"] is True
    assert result["top_k"] == 2
    assert result["results"][0]["item"]["id"] == "sqli-prior"
    assert calls["retrieve"]["top_k"] == 2
    assert "sqlite" in calls["retrieve"]["query"]
    assert "The search parameter may be injectable" in calls["retrieve"]["query"]
    assert result["metadata"] == {
        "challenge_type": "WEB_TARGET",
        "target_url": "http://example.test:8080/login",
        "vulnerability_type": "SQLI",
        "current_phase": "RECON",
        "allowed_hosts": ["example.test"],
    }
    assert result["searched_at"] == "2024-01-01T00:00:00"
    assert "not VerifiedFact evidence" in result["warning"]
    serialized = json.dumps(result, ensure_ascii=False)
    assert "flag{do-not-leak}" not in serialized
    assert "ground_truth" not in serialized
    assert "solution" not in serialized


def test_real_corpus_ranks_category_and_chinese_semantics_above_generic_web_metadata():
    fixtures = [
        ("模板", "server-side template injection SSTI Jinja2", "ssti-jinja2-detection"),
        ("连通性诊断", "OS command injection network diagnostic", "command-injection-parameter"),
        ("日志导出历史对象", "insecure direct object reference authorization", "idor-object-reference"),
        ("预览路径", "path traversal file read", "path-traversal-file-read"),
    ]
    for name, category_terms, expected_id in fixtures:
        challenge = SimpleNamespace(
            name=name,
            description=f"Authorized lab feature related to {category_terms}.",
            challenge_type="WEB_TARGET",
            target_url="http://example.test/web/target",
            allowed_hosts=["example.test"],
            metadata_json={},
        )
        result = KnowledgeContextService.build(
            SimpleNamespace(hints_json={"knowledge_mode": "on"}),
            challenge,
        )

        assert result["enabled"] is True
        assert result["results"], name
        assert any(item["item"]["id"] == expected_id for item in result["results"][:3]), (
            name,
            [(item["item"]["id"], item["score"]) for item in result["results"]],
        )


def test_explicit_vulnerability_metadata_gives_exact_category_strong_boost():
    generic = _item(
        "generic-web",
        title="Generic web target inspection",
        technique="HTTP inspection",
        summary="Inspect a web target with normal requests.",
    )
    exact = _item(
        "sqli-exact",
        title="Boolean SQL injection",
        technique="sql-injection",
        summary="Use a stable SQL injection oracle.",
    )
    retriever = KnowledgeRetriever(
        items=[generic, exact],
        config=KnowledgeConfig(mode="on", root=Path("."), top_k=2),
    )

    results = retriever.retrieve(
        query="web target",
        metadata={"challenge_type": "WEB_TARGET", "vulnerability_type": "SQL_INJECTION"},
        top_k=2,
    )

    # Route-only query terms are deliberately discarded, so metadata-only
    # overlap must not rescue a generic web item into the result set.
    assert [result.item.id for result in results] == ["sqli-exact"]


def test_context_service_retrieves_real_curated_corpus_without_database(monkeypatch):
    monkeypatch.setenv("C7F_KNOWLEDGE_MODE", "off")
    monkeypatch.setenv("C7F_KNOWLEDGE_TOP_K", "3")
    monkeypatch.delenv("C7F_KNOWLEDGE_ROOT", raising=False)

    challenge = SimpleNamespace(
        name="SQL injection boolean fixture",
        description="Test a login parameter with stable true and false predicates.",
        challenge_type="WEB_TARGET",
        target_url="http://example.test/login",
        allowed_hosts=["example.test"],
        metadata_json={},
    )

    result = KnowledgeContextService.build(
        SimpleNamespace(hints_json={"knowledge_mode": "on"}),
        challenge,
    )

    assert result["mode"] == "on"
    assert result["enabled"] is True
    assert result["top_k"] == 3
    assert result["run_override_applied"] is True
    assert result["results"]
    assert any(
        item["item"]["id"] == "sqli-boolean-blind"
        for item in result["results"]
    )
    assert "待验证先验" in result["formatted_prior"]
    assert "not VerifiedFact" in result["warning"]


def test_context_service_falls_back_safely_when_retrieval_fails(monkeypatch):
    def fail_config():
        raise RuntimeError("knowledge store unavailable")

    monkeypatch.setattr(knowledge_context, "load_knowledge_config", fail_config)

    result = KnowledgeContextService.build(
        SimpleNamespace(hints_json={"knowledge_mode": "on"}),
        SimpleNamespace(),
    )

    assert result["mode"] == "on"
    assert result["enabled"] is False
    assert result["on"] is True
    assert result["error"] == "RuntimeError: knowledge store unavailable"


@pytest.mark.asyncio
async def test_context_builder_exposes_top_level_knowledge_augmentation(monkeypatch):
    import app.services.context_builder as context_builder_module

    class EmptyRows:
        def all(self):
            return []

    class FakeSession:
        async def scalar(self, *_args, **_kwargs):
            return None

        async def scalars(self, *_args, **_kwargs):
            return EmptyRows()

    state = SimpleNamespace(
        security_context_json={},
        current_phase="RECON",
        active_skill_ids_json=[],
        skill_recommendations_json=[],
        confirmed_facts_json=[],
        run_plan_json={},
        attack_chain_plan_json={},
        capability_ledger_json={},
        rejected_paths_json=[],
        active_hypotheses_json=[],
        action_fingerprints_json={},
        attack_strategy_history_json=[],
        last_experiment_json={},
        no_progress_count=0,
        last_result_classification=None,
        finish_rejection_count=0,
        force_plan_action=False,
        duplicate_action_streak=0,
        control_rejection_streak=0,
        schema_error_streak=0,
        investigation_no_progress_count=0,
        degraded_action_streak=0,
        last_progress_at=None,
        read_files_json=[],
        read_ranges_json=[],
    )
    run = SimpleNamespace(
        id="run-1",
        max_context_observations=4,
        hints_json={"knowledge_mode": "off"},
        role_snapshot_json={},
        current_phase="RECON",
        agent_step_count=0,
        run_total_agent_steps=0,
        max_agent_steps=20,
        tool_call_count=0,
        run_total_logical_tool_calls=0,
        max_tool_calls=20,
        attempt_agent_steps=0,
        attempt_logical_tool_calls=0,
        checkpoint_segment_steps=0,
        current_attempt_number=1,
        max_runtime_seconds=60,
        started_at=None,
        recovery_checkpoint_json={},
        conversation_summary=None,
    )
    challenge = SimpleNamespace(
        id="challenge-1",
        name="Offline fixture",
        description="No network execution.",
        challenge_type="WEB_TARGET",
        target_url="http://example.test",
        allowed_hosts=["example.test"],
        flag_pattern="flag\\{.*\\}",
    )

    async def fake_load(_session, _run_id):
        return state

    async def fake_tools(*_args, **_kwargs):
        return set()

    async def fake_catalog(*_args, **_kwargs):
        return []

    async def fake_lessons(*_args, **_kwargs):
        return {}

    sentinel = {"mode": "off", "enabled": False}
    monkeypatch.setattr(context_builder_module.solver_state_service, "load", fake_load)
    monkeypatch.setattr(
        context_builder_module.vulnerability_lifecycle_engine,
        "evaluate",
        lambda _context: {},
    )
    monkeypatch.setattr(context_builder_module, "effective_tools_for", fake_tools)
    monkeypatch.setattr(context_builder_module, "load_tool_definitions", lambda: {})
    monkeypatch.setattr(context_builder_module, "specialist_skill_catalog", fake_catalog)
    monkeypatch.setattr(
        context_builder_module.challenge_lesson_service,
        "for_context",
        fake_lessons,
    )
    monkeypatch.setattr(
        context_builder_module.knowledge_context_service,
        "build",
        lambda *_args, **_kwargs: sentinel,
    )

    messages = await context_builder_module.context_builder.build(
        FakeSession(),
        run,
        challenge,
    )
    payload = json.loads(messages[1]["content"])

    assert payload["Knowledge Augmentation"] == sentinel
