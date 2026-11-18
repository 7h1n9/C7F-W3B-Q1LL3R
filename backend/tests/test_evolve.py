"""Tests for the offline scaffold evolver.

The corpus here is synthetic on purpose.  The real corpus lives in a 68 MB
export and changes whenever a new batch finishes; a test that reads it would
be slow and would change verdicts without anybody touching the code.  These
tests pin the *mechanism* (determinism, operator preconditions, similarity
arithmetic), not the numbers in the report.
"""

from __future__ import annotations

import json

import pytest

from app.training.diagnosis import LAYERS, LD, LI, LP, LS, Diagnosis, Evidence
from app.training.evolve import (
    MIN_PARENT_SIMILARITY,
    SEED_LAYERS,
    LayerText,
    Rule,
    added_rules_text,
    char_ngrams,
    content_digest,
    corpus_digest,
    cosine,
    densify,
    dumps,
    evolve,
    is_conditional,
    parent_similarity,
    policy_version,
    prune,
    render_layer,
    render_scaffold,
    reorder,
    similarity_report,
    split,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _diagnosis(
    run_id: str,
    layer: str,
    *,
    n: int = 40,
    facts: int = 2,
    repeat: int = 6,
    streak: int = 0,
    interface: dict[str, int] | None = None,
    score: float = 50.0,
    evidence: str = "curl -sS http://target/ | head",
) -> Diagnosis:
    features = {
        "n": n,
        "facts_total": facts,
        "repeat_pairs": repeat,
        "zero_fact_streak": streak,
        "fact_grounded": 0,
        "empty_ratio": 0.2,
        "enum_ratio": 0.1,
        "dead_total": 0,
        "target_first": 0,
        "fact_stop_ratio": 0.8,
        "interface_hits": interface or {},
    }
    counterfactual = {
        LS: "改写开局策略：首条命令直接打目标而不是本地暖场，例如 `curl -sS -i -m 20 '{{target_url}}/'`",
        LI: "先做一次接口自检再继续攻击：`ls -la {{linux_path}}; command -v curl php python3`",
        LD: "停止枚举，改读配方后只发一条判定性请求：`curl -sS -i -m 20 '{{target_url}}'`",
        LP: "把观测拉回 fact 通道：先跑 `curl -s -o /dev/null -w '%{http_code}'` 再用 `write-fact` 落地",
    }[layer]
    return Diagnosis(
        layer=layer,
        root_cause=f"{layer} 层：测试根因",
        counterfactual=counterfactual,
        blocking_hypothesis="测试阻塞假设",
        progress_score=score,
        confidence=0.6,
        evidence=(Evidence(event_index=0, event_id=f"ev-{run_id}", kind=f"{layer.lower()}:test", excerpt=evidence),),
        layer_scores={name: 1.0 for name in LAYERS},
        features=features,
        run_id=run_id,
    )


def corpus() -> list[Diagnosis]:
    return [
        _diagnosis("run-ld-1", LD, facts=1, repeat=80, score=52.0, evidence="ls -la /app; cat shared/*"),
        _diagnosis("run-ld-2", LD, facts=3, repeat=12, score=45.0, evidence="nmap -sV 10.0.0.2"),
        _diagnosis("run-lp-1", LP, facts=0, streak=9, score=41.0, evidence="head: cannot open result.txt"),
        _diagnosis("run-lp-2", LP, facts=4, streak=0, score=62.0, evidence="python3 blackboard.py write-fact"),
        _diagnosis("run-li-1", LI, interface={"tool_missing": 2}, score=55.0, evidence="bc: command not found"),
        _diagnosis("run-li-2", LI, interface={"path_missing": 3}, score=48.0),
        _diagnosis("run-ls-1", LS, facts=0, score=58.0, evidence="cat AGENTS.md"),
        _diagnosis("run-ls-2", LS, facts=1, score=44.0),
    ]


def _layer(*rules: tuple[str, str]) -> LayerText:
    return LayerText(
        layer=LD,
        rules=tuple(Rule(rule_id=rid, text=text) for rid, text in rules),
    )


# ---------------------------------------------------------------------------
# Determinism and identity
# ---------------------------------------------------------------------------


def test_evolve_is_byte_identical_across_runs_and_input_order() -> None:
    forward = evolve(corpus(), generations=3, population=4)
    shuffled = evolve(list(reversed(corpus())), generations=3, population=4)
    assert dumps(forward) == dumps(shuffled)


def test_dumps_round_trips_as_json() -> None:
    payload = json.loads(dumps(evolve(corpus(), generations=2, population=3)))
    assert payload["generations"][0]["generation"] == 1
    assert payload["limits"]["ship_ready"] is False


def test_policy_version_tracks_content_and_corpus() -> None:
    base = policy_version(SEED_LAYERS, corpus())
    assert base.startswith("pol-") and len(base) == len("pol-") + 10

    edited = dict(SEED_LAYERS)
    edited[LD] = LayerText(
        layer=LD,
        rules=SEED_LAYERS[LD].rules[:-1] + (Rule("ld.extra", "新增一条规则。"),),
    )
    assert policy_version(edited, corpus()) != base

    trimmed = corpus()[:-1]
    assert policy_version(SEED_LAYERS, trimmed) != base
    assert policy_version(SEED_LAYERS, corpus()) == base


def test_content_digest_ignores_rule_identity_but_not_text() -> None:
    one = _layer(("a", "同一句话。"))
    two = LayerText(layer=LD, rules=(Rule("b", "同一句话。"),))
    assert render_layer(one) == render_layer(two)

    same = dict(SEED_LAYERS)
    same[LD] = LayerText(layer=LD, rules=(Rule("seed-copy", SEED_LAYERS[LD].rules[0].text),) + SEED_LAYERS[LD].rules[1:])
    assert content_digest(same) == content_digest(SEED_LAYERS)

    changed = dict(SEED_LAYERS)
    changed[LD] = LayerText(
        layer=LD,
        rules=(Rule("seed-copy", SEED_LAYERS[LD].rules[0].text + "多一个字符"),)
        + SEED_LAYERS[LD].rules[1:],
    )
    assert content_digest(changed) != content_digest(SEED_LAYERS)


def test_corpus_digest_is_order_independent() -> None:
    assert corpus_digest(corpus()) == corpus_digest(list(reversed(corpus())))


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------


def test_densify_records_run_state_and_evidence() -> None:
    layer = _layer(("ld.keep", "保留一条基准规则。"))
    diagnosis = corpus()[0]
    result = densify(layer, [diagnosis])

    assert result.applied is True
    assert len(result.layer_text.rules) == 2
    added = result.layer_text.rules[-1]
    assert added.rule_id.startswith(f"{LD.lower()}.dn.{diagnosis.run_id[:8]}")
    # The rule must be checkable without the corpus: run id, its counters, its
    # first evidence excerpt, and the counterfactual all have to be in it.
    assert diagnosis.run_id[:8] in added.text
    assert "facts 1" in added.text
    assert "ls -la /app" in added.text
    assert "停止枚举" in added.text
    assert result.evidence_run_ids == (diagnosis.run_id,)


def test_densify_does_not_duplicate_the_same_rule() -> None:
    layer = _layer(("ld.keep", "保留一条基准规则。"))
    diagnosis = corpus()[0]
    once = densify(layer, [diagnosis])
    twice = densify(once.layer_text, [diagnosis])
    assert twice.applied is False
    assert len(twice.layer_text.rules) == len(once.layer_text.rules)


def test_prune_does_nothing_without_noise_evidence() -> None:
    layer = SEED_LAYERS[LP]
    quiet = [_diagnosis("run-quiet", LP, facts=6, streak=0)]
    result = prune(layer, quiet)
    assert result.applied is False
    assert result.layer_text == layer
    assert result.changed_rule_ids == ()


def test_prune_removes_only_conditionals_and_keeps_the_layer_non_empty() -> None:
    layer = SEED_LAYERS[LP]
    noisy = [_diagnosis("run-noisy", LP, facts=0, streak=9)]
    result = prune(layer, noisy)

    assert result.applied is True
    assert 0 < len(result.layer_text.rules) < len(layer.rules)
    assert not any(is_conditional(rule.text) for rule in result.layer_text.rules)
    removed = set(result.changed_rule_ids)
    assert removed == {rule.rule_id for rule in layer.rules if is_conditional(rule.text)}


def test_reorder_promotes_the_violated_rule_to_the_front() -> None:
    layer = SEED_LAYERS[LI]
    broken = [_diagnosis("run-li-x", LI, interface={"tool_missing": 3})]
    result = reorder(layer, broken)

    assert result.applied is True
    first = result.layer_text.rules[0]
    assert "tool-probe" in first.tags
    # A rotation, not a shuffle: the same rules, same relative order otherwise.
    assert {rule.rule_id for rule in result.layer_text.rules} == {
        rule.rule_id for rule in layer.rules
    }


def test_reorder_is_a_noop_when_nothing_matches() -> None:
    layer = SEED_LAYERS[LS]
    quiet = [_diagnosis("run-ls-y", LS, facts=5)]
    quiet[0] = Diagnosis(
        **{
            **quiet[0].__dict__,
            "features": {**quiet[0].features, "target_first": None, "n": 3},
        }
    )
    result = reorder(layer, quiet)
    assert result.applied is False


def test_split_moves_the_tail_to_reference_without_deleting() -> None:
    layer = LayerText(
        layer=LD,
        rules=tuple(Rule(f"r{i}", f"第 {i} 条规则。") for i in range(7)),
    )
    result = split(layer, corpus(), threshold=5)

    assert result.applied is True
    assert len(result.layer_text.rules) == 7
    assert len(result.layer_text.must) == 5
    assert len(result.layer_text.reference) == 2
    assert "## 参考" in render_layer(result.layer_text)


def test_split_is_a_noop_at_or_below_threshold() -> None:
    layer = _layer(("a", "一。"), ("b", "二。"))
    result = split(layer, corpus(), threshold=6)
    assert result.applied is False


# ---------------------------------------------------------------------------
# Similarity arithmetic
# ---------------------------------------------------------------------------


def test_char_ngrams_collapses_whitespace() -> None:
    assert char_ngrams("a b\n\tc", 3) == char_ngrams("a b c", 3)


def test_cosine_known_values() -> None:
    assert cosine("abcdef", "abcdef") == pytest.approx(1.0)
    assert cosine("", "") == pytest.approx(1.0)
    assert cosine("abc", "") == pytest.approx(0.0)
    # {abc, bcd, cde, def} vs {abc, bcx, cxy, xyz}: one shared trigram, both
    # norms 2 -> 1 / (2 * 2) = 0.25
    assert cosine("abcdef", "abcxyz") == pytest.approx(0.25)
    assert cosine("甲" * 50, "乙" * 50) == pytest.approx(0.0)


def test_similarity_report_shape_and_ordering() -> None:
    result = evolve(corpus(), generations=1, population=4)
    report = similarity_report(result.generations[0].variants)
    assert int(report["scaffold"]["n_pairs"]) == 6
    assert report["scaffold"]["min"] <= report["scaffold"]["median"] <= report["scaffold"]["max"]
    assert set(report["per_layer"]) == set(LAYERS)


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def test_three_generations_produce_the_requested_population() -> None:
    result = evolve(corpus(), generations=3, population=4)
    assert len(result.generations) == 3
    for generation in result.generations:
        assert generation.variants, f"gen{generation.generation} is empty"
        for variant in generation.variants:
            assert set(variant.layers) == set(LAYERS)


def test_layer_activation_is_not_all_knowledge() -> None:
    result = evolve(corpus(), generations=3, population=4)
    activation = {layer: 0 for layer in LAYERS}
    for generation in result.generations:
        for variant in generation.variants:
            for layer in variant.mutated_layers:
                activation[layer] += 1
    assert activation[LD] < sum(activation.values())
    assert activation[LS] > 0 and activation[LI] > 0 and activation[LP] > 0


def test_variant_evidence_is_a_subset_of_the_corpus() -> None:
    known = {diagnosis.run_id for diagnosis in corpus()}
    result = evolve(corpus(), generations=3, population=4)
    for generation in result.generations:
        for variant in generation.variants:
            assert set(variant.evidence_run_ids) <= known
            assert variant.evidence_run_ids == tuple(sorted(variant.evidence_run_ids))


def test_variants_differ_from_the_seed_seed_is_not_rewritten() -> None:
    seed_text = render_scaffold(SEED_LAYERS)
    result = evolve(corpus(), generations=1, population=4)
    for variant in result.generations[0].variants:
        assert render_scaffold(variant.layers) != seed_text
    # The module-level seed must stay untouched by a run.
    assert render_scaffold(SEED_LAYERS) == seed_text


def test_later_generations_name_a_parent_from_the_previous_generation() -> None:
    result = evolve(corpus(), generations=3, population=4)
    previous = {variant.variant_id for variant in result.generations[0].variants}
    for generation in result.generations[1:]:
        for variant in generation.variants:
            assert variant.parent_id in previous
        previous = {variant.variant_id for variant in generation.variants}


# ---------------------------------------------------------------------------
# Parent-similarity gate
# ---------------------------------------------------------------------------


def test_parent_similarity_is_one_for_an_unchanged_scaffold() -> None:
    assert parent_similarity(SEED_LAYERS, SEED_LAYERS) == pytest.approx(1.0)
    assert MIN_PARENT_SIMILARITY == 0.4


def test_accepted_variants_record_a_similarity_above_the_floor() -> None:
    result = evolve(corpus(), generations=3, population=4)
    assert result.parent_similarity_floor == MIN_PARENT_SIMILARITY
    for generation in result.generations:
        assert generation.rejected == ()
        for variant in generation.variants:
            assert variant.parent_similarity is not None
            assert variant.parent_similarity >= MIN_PARENT_SIMILARITY


def test_gate_rejects_every_candidate_that_drifts_below_the_floor() -> None:
    # A floor at 0.999 is unreachable for a mutation that adds rules, so every
    # generation-1 candidate must be rejected rather than written.
    result = evolve(
        corpus(), generations=1, population=4, parent_similarity_floor=0.999
    )
    generation = result.generations[0]
    assert generation.variants == ()
    assert len(generation.rejected) == 4
    for rejection in generation.rejected:
        assert rejection.similarity < 0.999
        assert rejection.floor == 0.999
        assert rejection.parent_id == "seed"
        assert "floor" in rejection.to_dict()["reason"]


def test_rejection_does_not_fabricate_a_next_generation() -> None:
    result = evolve(
        corpus(), generations=3, population=4, parent_similarity_floor=0.999
    )
    assert result.generations[0].variants == ()
    for generation in result.generations[1:]:
        assert generation.variants == ()
        assert generation.rejected == ()


def test_rejections_are_reported_in_the_json() -> None:
    payload = json.loads(
        dumps(evolve(corpus(), generations=1, population=2, parent_similarity_floor=0.999))
    )
    assert payload["parent_similarity_floor"] == 0.999
    assert len(payload["generations"][0]["rejected"]) == 2
    assert payload["generations"][0]["variants"] == []


# ---------------------------------------------------------------------------
# Diversity report: a diagnosis, not a pass/fail flag
# ---------------------------------------------------------------------------


def test_added_rules_text_excludes_the_seed() -> None:
    assert added_rules_text(SEED_LAYERS) == ""
    result = evolve(corpus(), generations=1, population=2)
    variant = result.generations[0].variants[0]
    added = added_rules_text(variant.layers)
    assert added
    assert added not in render_scaffold(SEED_LAYERS)


def test_diversity_block_reports_the_diagnosis_and_is_deterministic() -> None:
    first = evolve(corpus(), generations=3, population=4)
    second = evolve(corpus(), generations=3, population=4)
    # The report itself must be reproducible, not only the variant files.
    assert dumps(first) == dumps(second)

    block = first.generations[0].diversity
    assert block["seed_chars"] == len(render_scaffold(SEED_LAYERS))
    assert block["accepted"] == 4
    assert 0.0 <= block["min_full_cosine"] <= 1.0
    assert block["seed_overlap"]["n_pairs"] == 4.0
    # Regression of the observed ordering: the variants are at least as close
    # to each other as to the seed they inherited.  If a future operator makes
    # them diverge, this fires and the verdict text has to be revisited.
    assert block["min_full_cosine"] >= block["seed_overlap"]["min"]
    added = block["pairwise_added_rule_cosine"]
    assert added["n_pairs"] == 6.0
    assert added["min"] <= added["median"] <= added["max"]
    assert block["min_added_rule_cosine"] == added["min"]
    assert block["ship_ready"] is False
    assert "near-duplicates" in block["verdict"]
    # The same block reaches the artifact readers actually open.
    assert first.generations[0].to_dict()["diversity"]["verdict"] == block["verdict"]


def test_diversity_block_refuses_to_call_one_variant_a_population() -> None:
    result = evolve(corpus(), generations=1, population=1)
    block = result.generations[0].diversity
    assert block["accepted"] == 1
    assert block["min_full_cosine"] == 0.0
    assert "unmeasurable" in block["verdict"]
    assert block["ship_ready"] is False


def test_variant_json_carries_rule_origin() -> None:
    payload = json.loads(dumps(evolve(corpus(), generations=1, population=2)))
    origins = {
        rule["origin"]
        for variant in payload["generations"][0]["variants"]
        for layer in LAYERS
        for rule in variant["layers"][layer]["rules"]
    }
    assert origins == {"seed", "generated"}
