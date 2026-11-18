"""Offline scaffold evolution, first draft: deterministic, model-free, offline.

What this is
------------
The idea is borrowed from CyberEvolver (arXiv 2605.26195): what evolves is the
**agent scaffold**, not the model weights.  The scaffold is split into the same
four layers the diagnoser already attributes failures to -- ``LS`` strategy,
``LI`` interface, ``LD`` knowledge, ``LP`` perception -- and each failure
diagnosis becomes the input of a mutation that rewrites one layer.

What this is *not*
------------------
**This is a skeleton, not a working evolver.**  Three things are deliberately
missing and must not be papered over:

1. **No model call, no network, no rollout.**  Every mutation is a pure
   function of (layer text, diagnoses).  The reason is cost and
   reproducibility: the first thing to prove is that the loop runs and its
   output is auditable.  Nothing here has been executed against a target.
2. **Selection is static, so it is not selection on performance.**  Fitness
   counts how much of the diagnosis corpus a variant *addresses* (layer
   coverage, cited run ids, whether the counterfactual's own command appears in
   the text).  It never observes a solve.
3. **``progress_score`` is a weak proxy and is used only to order siblings.**
   On this corpus solved runs average 57.5 and unsolved 48.8 with heavily
   overlapping distributions, so ranking candidates by it is close to ranking
   by noise.  The docstring of
   :func:`app.training.diagnosis.progress_score` already forbids reading it as
   a capability number; this module inherits that ban.

**Variants produced here must not be shipped as-is.**  P3.4 is the gate: a
fixed-budget A/B against the seed scaffold.  Until that has run, a variant is
a hypothesis about the scaffold, not an improvement.

Determinism
-----------
Output is byte-identical for identical input:

* no clock, no ``random``, no set iteration in any output position;
* all corpus traversals go through ``sorted(...)``;
* evidence sampling is a strided index over the sorted pool, not a shuffle;
* JSON is written with ``sort_keys=True`` and ``ensure_ascii=False``.

Where a variant's rule ordering could depend on dict insertion order it does
not: layers are always emitted in :data:`LAYERS` order.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Sequence

from app.training.diagnosis import (
    LAYER_NAMES,
    LAYERS,
    LD,
    LI,
    LP,
    LS,
    Diagnosis,
)

# ---------------------------------------------------------------------------
# Seed scaffold
# ---------------------------------------------------------------------------
#
# The seed is data, not code: four ordered rule lists, no production import
# points, nothing that a running worker would pick up.  Text is a paraphrase of
# this repository's own operating discipline (first contact before recon,
# forward-slash container paths, playbook-first, facts carry command
# provenance), written for this module -- no text is copied from
# ``data/sources/cyberevolver``, which carries no license.
#
# ``Rule.group`` is what ``split`` flips from "must" to "reference"; ``tags``
# is what ``reorder`` uses to find the rule a diagnosis says is being violated.


@dataclass(frozen=True)
class Rule:
    rule_id: str
    text: str
    tags: tuple[str, ...] = ()
    group: str = "must"
    #: ``seed`` for the four seed layers, ``generated`` for anything an
    #: operator added.  The diversity report needs this to answer "how much of
    #: this variant is new" without re-deriving it from rule ids.
    origin: str = "seed"


@dataclass(frozen=True)
class LayerText:
    layer: str
    rules: tuple[Rule, ...]

    @property
    def must(self) -> tuple[Rule, ...]:
        return tuple(rule for rule in self.rules if rule.group == "must")

    @property
    def reference(self) -> tuple[Rule, ...]:
        return tuple(rule for rule in self.rules if rule.group == "reference")


def _seed(entries: Sequence[tuple[str, str, tuple[str, ...]]]) -> tuple[Rule, ...]:
    return tuple(Rule(rule_id=rid, text=text, tags=tags) for rid, text, tags in entries)


SEED_RULES: dict[str, tuple[Rule, ...]] = {
    LS: _seed(
        (
            (
                "ls.first-contact",
                "第 1 条命令就发向目标：`curl -sS -i -m 20 '{{target_url}}/' | head -60`，先确认目标在线再谈侦察。",
                ("first-contact",),
            ),
            (
                "ls.step-budget",
                "把 8 步当硬预算：8 步内没有一条命令命中目标，立刻改写假设，不要继续本地暖场。",
                ("budget", "first-contact"),
            ),
            (
                "ls.one-hypothesis",
                "每轮只保留一个待验证假设，先写 `HYPOTHESIS=...` 与 `TEST=...` 两行，再发对应的那一条命令。",
                ("hypothesis",),
            ),
            (
                "ls.no-local-substitute",
                "本地文件（README / AGENTS.md / writeup）只用来对齐题目，不允许代替对目标的实测。",
                ("first-contact",),
            ),
            (
                "ls.pivot-on-deadend",
                "连续 3 条命令没有新事实就换攻击面（认证 → 文件 → 注入），不要在同一个端点换参数。",
                ("deadend",),
            ),
            (
                "ls.flag-format",
                "疑似 flag 先比对题目给出的格式再提交，格式不符按未解处理。",
                ("flag",),
            ),
            (
                "ls.record-state",
                "每 5 步复述当前假设与已排除分支，防止上下文漂移。",
                ("hypothesis",),
            ),
            (
                "ls.abandon-rule",
                "若利用链已经超过 6 步仍未收敛，先写 `DEADEND=` 再决定是否放弃，不要无声切换。",
                ("deadend",),
            ),
        )
    ),
    LI: _seed(
        (
            (
                "li.self-check",
                "开局先跑接口自检：`ls -la {{linux_path}} 2>&1; command -v curl php python3; printf '%s' \"$PWD\"`。",
                ("self-check",),
            ),
            (
                "li.probe-tool",
                "依赖外部工具前先 `command -v <tool>`，缺失就走内置替代（python3 / curl）。",
                ("tool-probe",),
            ),
            (
                "li.forward-slash",
                "容器内路径一律用正斜杠；`{{linux_path}}` 不要拼反斜杠，反斜杠只出现在宿主侧。",
                ("path",),
            ),
            (
                "li.explicit-workdir",
                "每条命令自带工作目录：`cd {{linux_path}} && ...`，不依赖上一条命令的 cd。",
                ("path",),
            ),
            (
                "li.quote-payload",
                "带引号的 payload 用单引号包住，内部单引号写成 `'\\''`，不要用未转义的双引号拼 shell。",
                ("quoting",),
            ),
            (
                "li.exit-code",
                "关键命令后接 `; echo EXIT=$?`，把空观测与失败退出区分开。",
                ("self-check",),
            ),
            (
                "li.single-retry",
                "同一条坏命令最多重试一次；若第二次仍失败就换工具，不要换参数。",
                ("retry",),
            ),
            (
                "li.request-timeout",
                "外网请求统一加 `-m 20`，避免一条命令吃掉整个时间窗。",
                (),
            ),
        )
    ),
    LD: _seed(
        (
            (
                "ld.playbook-first",
                "先按 `data/knowledge/playbooks/*.jsonl` 的 `steps` / `known_failures` 选一条配方，再发命令；配方没有的端点不盲扫。",
                ("playbook",),
            ),
            (
                "ld.no-repeat",
                "同一请求族（同路径同参数）最多发 2 次；第 3 次必须改方法、编码或参数名。",
                ("no-repeat",),
            ),
            (
                "ld.deadend-record",
                "失败尝试写成 `DEADEND=<端点> <原因>`，下一条命令不得落在同一族。",
                ("deadend", "no-repeat"),
            ),
            (
                "ld.recipe-fields",
                "读配方只取三个字段：`preconditions`、`steps`、`known_failures`；`summary` 仅用于确认题目对应关系。",
                ("playbook",),
            ),
            (
                "ld.one-variant",
                "重复请求必须带一个变量（参数值、编码、Header），保证每次请求都在缩小假设空间。",
                ("no-repeat", "variant"),
            ),
            (
                "ld.enum-budget",
                "枚举类命令（ls / find / gobuster）若超过本轮到目前命令数的 30%，立即停扫换方向。",
                ("enum",),
            ),
            (
                "ld.verify-precondition",
                "配方命中后先用一条最小请求验证前置条件；若前置条件不成立就换配方，不要硬跑 steps。",
                ("playbook",),
            ),
            (
                "ld.land-fact",
                "解出一步就把命令原文与响应片段写进 fact，后续步骤只引用这条 fact。",
                ("deadend",),
            ),
        )
    ),
    LP: _seed(
        (
            (
                "lp.fact-attribution",
                "每条 fact 必须带命令归属：`write-fact \"...\" --from <命令序号>`，无归属的结论按噪声丢弃。",
                ("fact-attribution",),
            ),
            (
                "lp.keep-artifacts",
                "若响应超过 2000 字符，只保留含工件的行（状态码、路径、参数名、版本、报错原文），其余折叠成一行摘要。",
                ("keep-artifacts",),
            ),
            (
                "lp.min-observable",
                "先跑 `curl -s -o /dev/null -w '%{http_code}'` 证明观测能回传，再做长响应分析。",
                ("min-observable",),
            ),
            (
                "lp.diff-not-dump",
                "若同一端点已有历史响应，先做 diff 只把差异行送进上下文。",
                ("keep-artifacts",),
            ),
            (
                "lp.cite-fact",
                "引用 fact 必须回指它来自哪条命令的输出，不能把推断写成观测。",
                ("fact-attribution",),
            ),
            (
                "lp.no-fact-silence",
                "若连续 5 条命令 facts 不增长，停下检查 `result_excerpt` 是否被截断。",
                ("no-fact",),
            ),
            (
                "lp.verbatim-payload",
                "若 payload 超过 400 字符，原文逐字保留进工件表，hexdump 与栈回溯用占位符回填。",
                ("keep-artifacts",),
            ),
            (
                "lp.keep-numbers",
                "长度、状态码、时间戳原样保留，它们是区分两个响应的唯一依据。",
                ("keep-artifacts",),
            ),
        )
    ),
}

SEED_LAYERS: dict[str, LayerText] = {
    layer: LayerText(layer=layer, rules=SEED_RULES[layer]) for layer in LAYERS
}

#: Conditionals ``prune`` is allowed to delete.  Rule-level, not sentence-level:
#: a rule that only applies conditionally is exactly the kind of instruction
#: that inflates the prompt without changing the first command.
_CONDITIONAL_MARKERS = ("若", "如果", "除非", "一旦", "当 ")


def is_conditional(text: str) -> bool:
    return any(marker in text for marker in _CONDITIONAL_MARKERS)


# ---------------------------------------------------------------------------
# Rendering and identity
# ---------------------------------------------------------------------------


def render_layer(layer: LayerText) -> str:
    """Deterministic text for one layer.

    ``must`` rules are numbered so a reader can cite them; ``reference`` rules
    are bulleted and explicitly marked as not injected by default.  The two
    section headers are what makes ``split`` visible in the text instead of
    only in metadata.
    """

    lines = [f"## {layer.layer} {LAYER_NAMES.get(layer.layer, layer.layer)}"]
    must = layer.must
    if must:
        for position, rule in enumerate(must, start=1):
            lines.append(f"{position}. {rule.text}")
    else:
        lines.append("（必做组为空）")
    reference = layer.reference
    if reference:
        lines.append("## 参考（按需查阅，不默认注入）")
        for rule in reference:
            lines.append(f"- {rule.text}")
    return "\n".join(lines)


def render_scaffold(layers: Mapping[str, LayerText]) -> str:
    return "\n\n".join(render_layer(layers[layer]) for layer in LAYERS)


def content_digest(layers: Mapping[str, LayerText]) -> str:
    return hashlib.sha256(render_scaffold(layers).encode("utf-8")).hexdigest()[:12]


def corpus_digest(diagnoses: Iterable[Diagnosis]) -> str:
    """Stable digest of the diagnosis corpus a policy version was derived from.

    Ordering is by run id, and only the fields the mutators read are included,
    so re-running the diagnoser with a different ``window`` cannot change the
    version of an *identical* policy.
    """

    lines = []
    for diagnosis in sorted(diagnoses, key=lambda item: item.run_id):
        lines.append(
            "\t".join(
                (
                    diagnosis.run_id,
                    diagnosis.layer,
                    f"{diagnosis.progress_score:.2f}",
                    diagnosis.root_cause,
                    diagnosis.counterfactual,
                )
            )
        )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:12]


def policy_version(layers: Mapping[str, LayerText], diagnoses: Iterable[Diagnosis]) -> str:
    """``pol-xxxxxxxxxx``: content hash of the four layers plus the corpus.

    Both halves matter.  Two scaffolds with identical text derived from
    different diagnosis corpora are *not* the same policy version, because the
    provenance of a rule is part of the artifact readers have to be able to
    check.
    """

    payload = render_scaffold(layers) + "\n--corpus--\n" + corpus_digest(diagnoses)
    return "pol-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:10]


# ---------------------------------------------------------------------------
# Mutation operators
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OpResult:
    layer: str
    operator: str
    applied: bool
    reason: str
    before: int
    after: int
    changed_rule_ids: tuple[str, ...]
    evidence_run_ids: tuple[str, ...]
    layer_text: LayerText

    def to_dict(self) -> dict[str, Any]:
        return {
            "operator": self.operator,
            "layer": self.layer,
            "applied": self.applied,
            "reason": self.reason,
            "rules_before": self.before,
            "rules_after": self.after,
            "changed_rule_ids": list(self.changed_rule_ids),
            "evidence_run_ids": list(self.evidence_run_ids),
        }


def _directive(counterfactual: str) -> str:
    """The actionable head of a counterfactual, without the shared tail.

    The diagnoser's counterfactual is a fixed template per layer with the run's
    own URL substituted.  Keeping the whole template made every densified rule
    of a layer start with the same 80 characters, which is exactly the
    synonym-rewriting shape the plan warns about: three "different" rules that
    differ only in a URL.  Only the leading imperative is kept here; the run's
    own counters and evidence are prepended by :func:`densify`, so what makes a
    rule unique is its case, not its wording.
    """

    head = counterfactual.strip()
    for marker in ("之后", "；若", "——"):
        head = head.split(marker)[0]
    return head.strip()[:100]


def _evidence_line(diagnosis: Diagnosis) -> str:
    if diagnosis.evidence:
        return diagnosis.evidence[0].excerpt.strip()[:70]
    return diagnosis.root_cause.strip()[:70]


def densify(layer: LayerText, diagnoses: Sequence[Diagnosis]) -> OpResult:
    """Add the counterfactual a diagnosis asked for, with that run's own state.

    Trigger: at least one diagnosis in ``diagnoses``.  Each diagnosis yields at
    most one rule; identical texts are not appended twice.

    The rule carries three things a reader can check without opening the
    corpus: the run id it came from, that run's own trajectory counters, and
    the first evidence excerpt verbatim.  The last part matters -- the
    counterfactual itself is a per-layer template, so without the excerpt and
    the counters every densified rule of a layer would read the same and the
    operator really would be doing synonym rewriting.
    """

    before = len(layer.rules)
    existing = {rule.text for rule in layer.rules}
    added: list[Rule] = []
    cited: list[str] = []
    for diagnosis in diagnoses:
        directive = _directive(diagnosis.counterfactual)
        if not directive:
            continue
        features = diagnosis.features or {}
        text = (
            f"{layer.layer} 反例 {diagnosis.run_id[:8]}："
            f"{int(features.get('n') or 0)} 条命令 / facts {int(features.get('facts_total') or 0)} / "
            f"重复 {int(features.get('repeat_pairs') or 0)}，"
            f"`{_evidence_line(diagnosis)}` 之后没有新结论；改法：{directive}"
        )
        if text in existing:
            continue
        existing.add(text)
        added.append(
            Rule(
                rule_id=f"{layer.layer.lower()}.dn.{diagnosis.run_id[:8]}.{len(added)}",
                text=text,
                tags=("densified", f"from:{diagnosis.layer}"),
                origin="generated",
            )
        )
        cited.append(diagnosis.run_id)
    if not added:
        return OpResult(
            layer=layer.layer,
            operator="densify",
            applied=False,
            reason="no new directive (empty diagnoses or all texts already present)",
            before=before,
            after=before,
            changed_rule_ids=(),
            evidence_run_ids=(),
            layer_text=layer,
        )
    updated = replace(layer, rules=layer.rules + tuple(added))
    return OpResult(
        layer=layer.layer,
        operator="densify",
        applied=True,
        reason=f"added {len(added)} rule(s) from counterfactuals",
        before=before,
        after=len(updated.rules),
        changed_rule_ids=tuple(rule.rule_id for rule in added),
        evidence_run_ids=tuple(cited),
        layer_text=updated,
    )


def _noise_evidence(diagnoses: Sequence[Diagnosis]) -> bool:
    """Did a diagnosis actually say "the channel is producing nothing"?"""

    for diagnosis in diagnoses:
        features = diagnosis.features or {}
        if int(features.get("facts_total") or 0) == 0:
            return True
        if int(features.get("zero_fact_streak") or 0) >= 5:
            return True
    return False


def prune(layer: LayerText, diagnoses: Sequence[Diagnosis]) -> OpResult:
    """Drop conditional rules when a diagnosis reports a silent channel.

    Precondition matters more than the edit: with no diagnosis saying facts
    stopped growing, this operator does nothing rather than shrinking the
    prompt speculatively.  It also refuses to empty the layer -- a layer with
    no rules is not a scaffold, it is a bug.
    """

    before = len(layer.rules)
    if not diagnoses or not _noise_evidence(diagnoses):
        return OpResult(
            layer=layer.layer,
            operator="prune",
            applied=False,
            reason="no diagnosis reports facts=0 or a zero-fact streak >= 5",
            before=before,
            after=before,
            changed_rule_ids=(),
            evidence_run_ids=(),
            layer_text=layer,
        )
    removed = [rule for rule in layer.rules if is_conditional(rule.text)]
    kept = tuple(rule for rule in layer.rules if not is_conditional(rule.text))
    if not removed or not kept:
        return OpResult(
            layer=layer.layer,
            operator="prune",
            applied=False,
            reason="nothing removable (no conditional rules, or only conditional rules)",
            before=before,
            after=before,
            changed_rule_ids=(),
            evidence_run_ids=(),
            layer_text=layer,
        )
    return OpResult(
        layer=layer.layer,
        operator="prune",
        applied=True,
        reason=f"removed {len(removed)} conditional rule(s)",
        before=before,
        after=len(kept),
        changed_rule_ids=tuple(rule.rule_id for rule in removed),
        evidence_run_ids=tuple(d.run_id for d in diagnoses[:1]),
        layer_text=replace(layer, rules=kept),
    )


#: Which seed tags a diagnosis says are being violated.  Keyed by layer, then by
#: the feature that fired.  Anything not listed moves nothing.
_VIOLATED_TAGS: dict[str, dict[str, tuple[str, ...]]] = {
    LI: {
        "tool_missing": ("tool-probe", "self-check"),
        "path_missing": ("path",),
        "not_a_dir": ("path",),
        "shell_syntax": ("quoting",),
        "bad_option": ("quoting", "self-check"),
        "permission": ("self-check",),
    },
    LP: {
        "facts_total": ("no-fact", "min-observable"),
        "fact_grounded": ("fact-attribution", "cite"),
        "empty_ratio": ("keep-artifacts",),
    },
    LD: {
        "repeat_pairs": ("no-repeat", "variant"),
        "enum_ratio": ("enum",),
        "dead_total": ("deadend",),
    },
    LS: {
        "target_first": ("first-contact",),
        "fact_stop_ratio": ("hypothesis", "budget"),
    },
}


def violated_tags(layer: str, diagnoses: Sequence[Diagnosis]) -> tuple[str, ...]:
    """Tags the diagnostics blame, in first-seen order, deduplicated."""

    ordered: list[str] = []
    for diagnosis in diagnoses:
        features = diagnosis.features or {}
        mapping = _VIOLATED_TAGS.get(layer, {})
        keys: list[str] = []
        if layer == LI:
            keys = sorted((features.get("interface_hits") or {}).keys())
        elif layer == LP:
            if int(features.get("facts_total") or 0) == 0:
                keys.append("facts_total")
            if int(features.get("facts_total") or 0) >= 3 and int(features.get("fact_grounded") or 0) == 0:
                keys.append("fact_grounded")
            if float(features.get("empty_ratio") or 0) >= 0.55:
                keys.append("empty_ratio")
        elif layer == LD:
            if int(features.get("repeat_pairs") or 0) > 0:
                keys.append("repeat_pairs")
            if float(features.get("enum_ratio") or 0) >= 0.45:
                keys.append("enum_ratio")
            if int(features.get("dead_total") or 0) >= 3:
                keys.append("dead_total")
        elif layer == LS:
            if features.get("target_first") is not None:
                keys.append("target_first")
            elif int(features.get("n") or 0) >= 10:
                keys.append("target_first")
            if float(features.get("fact_stop_ratio") or 1.0) <= 0.5:
                keys.append("fact_stop_ratio")
        for key in keys:
            for tag in mapping.get(key, ()):
                if tag not in ordered:
                    ordered.append(tag)
    return tuple(ordered)


def reorder(layer: LayerText, diagnoses: Sequence[Diagnosis]) -> OpResult:
    """Move rules the diagnosis says were violated to the front of ``must``.

    Stable: within the promoted block the seed order is preserved, so the
    result is a rotation, never a shuffle.
    """

    before = len(layer.rules)
    tags = violated_tags(layer.layer, diagnoses)
    if not tags:
        return OpResult(
            layer=layer.layer,
            operator="reorder",
            applied=False,
            reason="no violated tag derivable from these diagnoses",
            before=before,
            after=before,
            changed_rule_ids=(),
            evidence_run_ids=(),
            layer_text=layer,
        )
    must = layer.must
    promoted = tuple(rule for rule in must if set(rule.tags) & set(tags))
    if not promoted or len(promoted) == len(must):
        return OpResult(
            layer=layer.layer,
            operator="reorder",
            applied=False,
            reason=f"tags {list(tags)} matched {len(promoted)} rule(s); nothing to move",
            before=before,
            after=before,
            changed_rule_ids=(),
            evidence_run_ids=(),
            layer_text=layer,
        )
    # Order the promoted block by *how directly* each rule answers the
    # diagnosis, then by seed position.  Sorting only by seed position put
    # ``li.self-check`` ahead of ``li.probe-tool`` for a `command not found`
    # diagnosis, because the general self-check rule happens to sit first in
    # the seed -- the promotion then looked like a no-op in review.
    rank = {tag: position for position, tag in enumerate(tags)}
    promoted = tuple(
        sorted(
            promoted,
            key=lambda rule: (
                min(rank.get(tag, len(rank)) for tag in rule.tags if tag in rank),
                must.index(rule),
            ),
        )
    )
    rest = tuple(rule for rule in must if rule not in promoted)
    if tuple(must) == promoted + rest:
        return OpResult(
            layer=layer.layer,
            operator="reorder",
            applied=False,
            reason="promoted block already leads the layer",
            before=before,
            after=before,
            changed_rule_ids=(),
            evidence_run_ids=(),
            layer_text=layer,
        )
    updated = replace(layer, rules=promoted + rest + layer.reference)
    return OpResult(
        layer=layer.layer,
        operator="reorder",
        applied=True,
        reason=f"promoted {len(promoted)} rule(s) matching {list(tags)}",
        before=before,
        after=len(updated.rules),
        changed_rule_ids=tuple(rule.rule_id for rule in promoted),
        evidence_run_ids=tuple(d.run_id for d in diagnoses[:1]),
        layer_text=updated,
    )


def split(
    layer: LayerText,
    diagnoses: Sequence[Diagnosis],
    *,
    threshold: int = 6,
) -> OpResult:
    """Split an over-long layer into an injected ``must`` set and a shelf.

    Trigger: more than ``threshold`` rules in the layer (``densify`` is what
    usually pushes a layer over).  Only the first ``threshold`` stay injected;
    the rest move to ``reference``.  Nothing is deleted, so this is reversible.
    """

    before = len(layer.rules)
    if len(layer.rules) <= threshold:
        return OpResult(
            layer=layer.layer,
            operator="split",
            applied=False,
            reason=f"{len(layer.rules)} rule(s) <= threshold {threshold}",
            before=before,
            after=before,
            changed_rule_ids=(),
            evidence_run_ids=(),
            layer_text=layer,
        )
    keep = tuple(layer.rules[:threshold])
    moved = tuple(replace(rule, group="reference") for rule in layer.rules[threshold:] if rule.group != "reference")
    already = tuple(rule for rule in layer.rules[threshold:] if rule.group == "reference")
    if not moved:
        return OpResult(
            layer=layer.layer,
            operator="split",
            applied=False,
            reason="tail is already in the reference group",
            before=before,
            after=before,
            changed_rule_ids=(),
            evidence_run_ids=(),
            layer_text=layer,
        )
    updated = replace(layer, rules=keep + moved + already)
    return OpResult(
        layer=layer.layer,
        operator="split",
        applied=True,
        reason=f"{threshold} injected, {len(moved) + len(already)} moved to reference",
        before=before,
        after=len(updated.rules),
        changed_rule_ids=tuple(rule.rule_id for rule in moved),
        evidence_run_ids=tuple(d.run_id for d in diagnoses[:1]),
        layer_text=updated,
    )


OPERATORS = {
    "densify": densify,
    "prune": prune,
    "reorder": reorder,
    "split": split,
}

#: Fixed order used when a generation has to pick "some other operator".
OP_CYCLE = ("densify", "reorder", "split", "prune")


# ---------------------------------------------------------------------------
# Static fitness and similarity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StaticFitness:
    """Fitness from the diagnosis corpus only.

    Every term is a count over an artifact that already exists on disk.  None
    of them observes whether the scaffold helps an agent solve anything, which
    is why :func:`evolve` writes them next to the variants instead of a
    "score".
    """

    score: float
    layer_coverage: float
    evidence_coverage: float
    directive_coverage: float
    mean_progress_score: float

    def to_dict(self) -> dict[str, float]:
        return {
            "score": round(self.score, 4),
            "layer_coverage": round(self.layer_coverage, 4),
            "evidence_coverage": round(self.evidence_coverage, 4),
            "directive_coverage": round(self.directive_coverage, 4),
            "mean_progress_score": round(self.mean_progress_score, 2),
        }


#: One signature substring per layer's counterfactual, taken from the
#: diagnoser's own wording.  If a variant's text contains it, the variant really
#: did encode that counterfactual rather than mention the layer by name.
_DIRECTIVE_SIGNATURES = {
    LS: "首条命令直接打目标",
    LI: "command -v curl php python3",
    LD: "按 playbook 的下一步分支",
    LP: "write-fact",
}


def static_fitness(
    layers: Mapping[str, LayerText],
    diagnoses: Sequence[Diagnosis],
    *,
    cited_run_ids: Sequence[str],
    mutated_layers: Sequence[str],
) -> StaticFitness:
    total = len(diagnoses) or 1
    text = render_scaffold(layers)
    layer_hits = sum(1 for diagnosis in diagnoses if diagnosis.layer in set(mutated_layers))
    cited = set(cited_run_ids)
    evidence_hits = sum(1 for diagnosis in diagnoses if diagnosis.run_id in cited)
    directive_hits = sum(
        1 for layer, signature in _DIRECTIVE_SIGNATURES.items() if signature in text
    )
    scores = [d.progress_score for d in diagnoses if d.run_id in cited]
    layer_coverage = layer_hits / total
    evidence_coverage = evidence_hits / total
    directive_coverage = directive_hits / len(_DIRECTIVE_SIGNATURES)
    score = 0.5 * evidence_coverage + 0.35 * layer_coverage + 0.15 * directive_coverage
    return StaticFitness(
        score=score,
        layer_coverage=layer_coverage,
        evidence_coverage=evidence_coverage,
        directive_coverage=directive_coverage,
        mean_progress_score=(sum(scores) / len(scores)) if scores else 0.0,
    )


def char_ngrams(text: str, n: int = 3) -> Counter:
    """Character n-grams with no padding, counted, whitespace collapsed.

    Collapsing whitespace keeps ``render``-level reindentation from reading as
    content change.  With ``len(text) < n`` the whole string is one gram, so a
    short variant is never indistinguishable from an empty one.
    """

    collapsed = " ".join(text.split())
    if not collapsed:
        return Counter()
    if len(collapsed) <= n:
        return Counter({collapsed: 1})
    return Counter(collapsed[i : i + n] for i in range(len(collapsed) - n + 1))


def cosine(left: str, right: str, n: int = 3) -> float:
    """Cosine over character n-gram counts.  Empty vs empty is 1.0."""

    a = char_ngrams(left, n)
    b = char_ngrams(right, n)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    shared = sum(count * b[gram] for gram, count in a.items() if gram in b)
    norm_a = math.sqrt(sum(count * count for count in a.values()))
    norm_b = math.sqrt(sum(count * count for count in b.values()))
    return shared / (norm_a * norm_b)


#: A mutation that drifts this far from its parent is not a variant of that
#: scaffold, it is a different scaffold wearing its name.  Such a candidate is
#: rejected instead of being written, because P3.4 would otherwise be comparing
#: the seed against something no longer derived from it.
#:
#: Note what this guard does *not* claim: on this corpus it never fires.  The
#: measured parent-child similarity is 0.963-0.973 (generation 2) and
#: 0.985-0.996 (generation 3) -- mutations here are small additions, not
#: rewrites.  The floor is a rail against a future operator that rewrites a
#: whole layer, and it is tested with a floor high enough to trigger.
MIN_PARENT_SIMILARITY = 0.4


def parent_similarity(
    layers: Mapping[str, LayerText], parent_layers: Mapping[str, LayerText], n: int = 3
) -> float:
    """Cosine between a candidate scaffold and the scaffold it was derived from."""

    return cosine(render_scaffold(layers), render_scaffold(parent_layers), n)


@dataclass(frozen=True)
class Rejection:
    """A candidate that never became a variant, and why."""

    variant_id: str
    parent_id: str
    similarity: float
    floor: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "variant_id": self.variant_id,
            "parent_id": self.parent_id,
            "parent_similarity": round(self.similarity, 4),
            "floor": self.floor,
            "reason": f"parent similarity {self.similarity:.3f} < floor {self.floor}",
        }


def _gate(
    variant_id: str,
    parent_id: str,
    layers: Mapping[str, LayerText],
    parent_layers: Mapping[str, LayerText],
    floor: float,
) -> tuple[bool, float, Rejection | None]:
    similarity = parent_similarity(layers, parent_layers)
    if similarity >= floor:
        return True, similarity, None
    return False, similarity, Rejection(variant_id, parent_id, similarity, floor)


def _summary(values: Sequence[float]) -> dict[str, float]:
    if not values:
        return {"min": 0.0, "median": 0.0, "max": 0.0, "n_pairs": 0.0}
    ordered = sorted(values)
    middle = len(ordered) // 2
    median = (
        ordered[middle]
        if len(ordered) % 2
        else (ordered[middle - 1] + ordered[middle]) / 2.0
    )
    return {
        "min": round(ordered[0], 4),
        "median": round(median, 4),
        "max": round(ordered[-1], 4),
        "n_pairs": float(len(ordered)),
    }


def similarity_report(variants: Sequence["Variant"], n: int = 3) -> dict[str, Any]:
    """Pairwise cosine inside one generation, full scaffold and per layer.

    Two numbers are reported on purpose.  ``scaffold`` compares the whole
    four-layer text; ``per_layer`` compares one layer at a time.  A generation
    that mutates only one layer per variant will look near-identical at
    scaffold level *because three layers were never touched*, which is not the
    same phenomenon as an operator rewriting one sentence into another.  The
    per-layer figure is the one that can distinguish those; the scaffold figure
    is the one the P3.3 acceptance criterion names, so both are printed.
    """

    pairs: list[tuple[str, str, float]] = []
    per_layer: dict[str, list[float]] = {layer: [] for layer in LAYERS}
    for i in range(len(variants)):
        for j in range(i + 1, len(variants)):
            left, right = variants[i], variants[j]
            pairs.append(
                (
                    left.variant_id,
                    right.variant_id,
                    round(cosine(render_scaffold(left.layers), render_scaffold(right.layers), n), 4),
                )
            )
            for layer in LAYERS:
                per_layer[layer].append(
                    round(cosine(render_layer(left.layers[layer]), render_layer(right.layers[layer]), n), 4)
                )
    values = [value for _, _, value in pairs]
    return {
        "scaffold": _summary(values),
        "per_layer": {layer: _summary(per_layer[layer]) for layer in LAYERS},
        "pairs": [{"left": a, "right": b, "cosine": v} for a, b, v in pairs],
    }


def added_rules_text(layers: Mapping[str, LayerText]) -> str:
    """Only the rules an operator produced, seed rules excluded."""

    return "\n".join(
        rule.text
        for layer in LAYERS
        for rule in layers[layer].rules
        if rule.origin != "seed"
    )


def diversity_report(
    variants: Sequence["Variant"],
    seed_layers: Mapping[str, LayerText],
    n: int = 3,
) -> dict[str, Any]:
    """Diversity as a *measurement with a verdict*, not a pass/fail flag.

    The P3.3 acceptance bar (pairwise cosine <= 0.4) is reported as failed
    rather than waived: the numbers below say why.  Three quantities are kept
    separate on purpose.

    ``min_full_cosine``
        Whole-scaffold pairwise cosine.  This is what the acceptance criterion
        names.
    ``seed_overlap``
        ``cosine(variant, seed)`` per variant: how much of the variant *is* the
        untouched seed.  Read it against ``min_full_cosine``.  A variant at
        0.78 from the seed that sits at 0.90 from its siblings is closer to
        them than to the scaffold it came from, so the extra similarity has to
        come from the additions, not from inherited text.

        A tighter-looking "floor" was tried first: count only the n-grams the
        two variants share *and* the seed contains.  It returned 0.23 for the
        numbers below, while a control of seed plus two fully disjoint 900-char
        additions scored 0.77 against the same seed.  The bound is therefore
        too loose to mean anything, and reporting it next to a 0.90 would have
        implied the seed explains almost none of the similarity.  It is not
        reported.
    ``pairwise_added_rule_cosine``
        The same cosine over *only* the rules an operator added.  This is the
        number that answers "is the mutation actually producing distinct
        hypotheses", and the one to read before trusting a generation.
    """

    seed_text = render_scaffold(seed_layers)
    seed_chars = len(seed_text)
    full = [render_scaffold(variant.layers) for variant in variants]
    added = [added_rules_text(variant.layers) for variant in variants]

    full_pairs: list[float] = []
    added_pairs: list[float] = []
    for i in range(len(variants)):
        for j in range(i + 1, len(variants)):
            full_pairs.append(cosine(full[i], full[j], n))
            added_pairs.append(cosine(added[i], added[j], n))
    seed_overlap = _summary([cosine(text, seed_text, n) for text in full])

    full_summary = _summary(full_pairs)
    added_summary = _summary(added_pairs)
    if len(variants) < 2:
        verdict = (
            f"only {len(variants)} variant(s) accepted: pairwise diversity is "
            "unmeasurable, and a single variant is not a population."
        )
    else:
        verdict = (
            f"{len(variants)} accepted variants share {seed_chars} chars of seed; "
            f"each is {seed_overlap['min']:.3f}-{seed_overlap['max']:.3f} similar to "
            f"the untouched seed, yet {full_summary['min']:.3f}-{full_summary['max']:.3f} "
            f"similar to each other and {added_summary['min']:.3f}-{added_summary['max']:.3f} "
            "on their added rules alone -- they are near-duplicates of each other, "
            "not independent hypotheses."
        )
    return {
        "seed_chars": seed_chars,
        "accepted": len(variants),
        "min_full_cosine": full_summary["min"],
        "seed_overlap": seed_overlap,
        "min_added_rule_cosine": added_summary["min"],
        "pairwise_added_rule_cosine": added_summary,
        "verdict": verdict,
        "ship_ready": False,
    }


# ---------------------------------------------------------------------------
# Variants and the loop
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Variant:
    variant_id: str
    generation: int
    parent_id: str | None
    layers: Mapping[str, LayerText]
    operators: tuple[str, ...]
    mutated_layers: tuple[str, ...]
    evidence_run_ids: tuple[str, ...]
    op_log: tuple[OpResult, ...]
    fitness: StaticFitness
    policy_version: str
    parent_similarity: float | None = None

    @property
    def digest(self) -> str:
        return content_digest(self.layers)

    def to_dict(self) -> dict[str, Any]:
        return {
            "variant_id": self.variant_id,
            "generation": self.generation,
            "parent_id": self.parent_id,
            "parent_similarity": (
                round(self.parent_similarity, 4) if self.parent_similarity is not None else None
            ),
            "policy_version": self.policy_version,
            "content_digest": self.digest,
            "operators": list(self.operators),
            "mutated_layers": list(self.mutated_layers),
            "evidence_run_ids": list(self.evidence_run_ids),
            "fitness": self.fitness.to_dict(),
            "limits": {
                "no_model_call": True,
                "no_rollout": True,
                "selection_basis": "static diagnosis coverage, not solve rate",
                "ship_ready": False,
            },
            "layers": {
                layer: {
                    "text": render_layer(self.layers[layer]),
                    "rules": [
                        {
                            "rule_id": rule.rule_id,
                            "text": rule.text,
                            "tags": list(rule.tags),
                            "group": rule.group,
                            "origin": rule.origin,
                        }
                        for rule in self.layers[layer].rules
                    ],
                }
                for layer in LAYERS
            },
            "op_log": [item.to_dict() for item in self.op_log],
        }


def _pool(diagnoses: Sequence[Diagnosis], layer: str) -> list[Diagnosis]:
    return [d for d in sorted(diagnoses, key=lambda item: item.run_id) if d.layer == layer]


def _sample(
    pool: Sequence[Diagnosis], offset: int, count: int, stride: int = 1
) -> tuple[Diagnosis, ...]:
    """Strided slice over a sorted pool -- deterministic, no RNG.

    ``stride`` is how the population is partitioned: with ``stride=population``
    and ``offset=variant_index`` sibling variants draw **disjoint** slices, so
    two variants of the same generation cannot converge just because they were
    fed the same five runs.  Determinism is preserved because the pool is
    sorted by run id before slicing.
    """

    if not pool or count <= 0:
        return ()
    start = offset % len(pool)
    picked: list[Diagnosis] = []
    for step in range(len(pool)):
        diagnosis = pool[(start + step * max(1, stride)) % len(pool)]
        if diagnosis not in picked:
            picked.append(diagnosis)
        if len(picked) >= count:
            break
    return tuple(picked)


def _apply(
    layers: Mapping[str, LayerText],
    layer: str,
    operator: str,
    diagnoses: Sequence[Diagnosis],
) -> tuple[Mapping[str, LayerText], OpResult]:
    result = OPERATORS[operator](layers[layer], diagnoses)
    updated = dict(layers)
    updated[layer] = result.layer_text
    return updated, result


def _ids(pool: Sequence[Diagnosis]) -> set[str]:
    return {diagnosis.run_id for diagnosis in pool}


def _build(
    *,
    variant_id: str,
    generation: int,
    parent_id: str | None,
    base: Mapping[str, LayerText],
    plan: Sequence[tuple[str, str]],
    pool: Mapping[str, Sequence[Diagnosis]],
    offset: int,
    count: int,
    corpus: Sequence[Diagnosis],
    fill_count: int = 0,
    stride: int = 1,
) -> Variant:
    layers = dict(base)
    log: list[OpResult] = []
    cited: list[str] = []
    mutated: list[str] = []
    operators: list[str] = []

    for index, (layer, operator) in enumerate(plan):
        diagnoses = _sample(pool[layer], offset + index, count, stride)
        layers, result = _apply(layers, layer, operator, diagnoses)
        log.append(result)
        operators.append(operator)
        if result.applied:
            mutated.append(layer)
        for run_id in result.evidence_run_ids:
            if run_id not in cited:
                cited.append(run_id)

    # Every layer the focus plan did not touch gets a light densify from that
    # variant's own slice of the corpus.  A variant is a whole scaffold
    # revision, not a one-layer patch: leaving three layers byte-identical to
    # the seed would make the population differ in one place and would say
    # nothing about the other three.  ``fill_count`` == 0 disables this, which
    # is what the similarity baseline in the report uses.
    for index, layer in enumerate(LAYERS):
        if layer in mutated or fill_count <= 0:
            continue
        diagnoses = _sample(pool[layer], offset + 4 + index, fill_count, stride)
        layers, result = _apply(layers, layer, "densify", diagnoses)
        log.append(result)
        operators.append("densify")
        if result.applied:
            mutated.append(layer)
        for run_id in result.evidence_run_ids:
            if run_id not in cited:
                cited.append(run_id)

    fitness = static_fitness(
        layers, corpus, cited_run_ids=cited, mutated_layers=mutated
    )
    return Variant(
        variant_id=variant_id,
        generation=generation,
        parent_id=parent_id,
        layers=layers,
        operators=tuple(operators),
        mutated_layers=tuple(layer for layer in LAYERS if layer in mutated),
        evidence_run_ids=tuple(sorted(cited)),
        op_log=tuple(log),
        fitness=fitness,
        policy_version=policy_version(layers, corpus),
    )


#: Generation-1 plan.  One layer per variant as the primary target, a different
#: operator pair per variant.  Every other layer is then filled from the same
#: variant's corpus slice (see ``_build``).
GEN1_PLAN: tuple[tuple[tuple[str, str], ...], ...] = (
    ((LD, "densify"), (LD, "reorder")),
    ((LP, "prune"), (LP, "densify")),
    ((LI, "densify"), (LI, "reorder")),
    ((LS, "densify"), (LS, "split")),
)


@dataclass(frozen=True)
class GenerationResult:
    generation: int
    variants: tuple[Variant, ...]
    similarity: Mapping[str, Any]
    rejected: tuple[Rejection, ...] = ()
    diversity: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "variants": [variant.to_dict() for variant in self.variants],
            "rejected": [item.to_dict() for item in self.rejected],
            "similarity": self.similarity,
            "diversity": self.diversity,
            "layer_activation": layer_activation(self.variants),
        }


@dataclass(frozen=True)
class EvolutionResult:
    seed_policy_version: str
    corpus_digest: str
    generations: tuple[GenerationResult, ...]
    diagnoses: tuple[Diagnosis, ...]
    parent_similarity_floor: float = MIN_PARENT_SIMILARITY

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed_policy_version": self.seed_policy_version,
            "corpus_digest": self.corpus_digest,
            "parent_similarity_floor": self.parent_similarity_floor,
            "diagnoses": len(self.diagnoses),
            "layer_distribution": layer_distribution(self.diagnoses),
            "generations": [generation.to_dict() for generation in self.generations],
            "limits": EVOLUTION_LIMITS,
        }


def layer_activation(variants: Sequence[Variant]) -> dict[str, int]:
    """How many variants in a generation actually changed each layer."""

    counts = {layer: 0 for layer in LAYERS}
    for variant in variants:
        for layer in variant.mutated_layers:
            counts[layer] += 1
    return counts


def layer_distribution(diagnoses: Iterable[Diagnosis]) -> dict[str, int]:
    counts = {layer: 0 for layer in LAYERS}
    for diagnosis in diagnoses:
        counts[diagnosis.layer] = counts.get(diagnosis.layer, 0) + 1
    return counts


EVOLUTION_LIMITS: dict[str, Any] = {
    "no_model_call": True,
    "no_rollout": True,
    "selection_basis": "static diagnosis coverage",
    "progress_score_correlation_with_solved": "weak (57.5 solved vs 48.8 unsolved, heavy overlap)",
    "ship_ready": False,
    "why": (
        "A variant is a hypothesis about the scaffold. Fitness never observes a "
        "solve, so selection is selection on diagnosis statistics, not on "
        "performance. P3.4's fixed-budget A/B is the gate before any variant is "
        "used."
    ),
}


def _select_parents(variants: Sequence[Variant], k: int) -> list[Variant]:
    """Top-k by ``progress_score`` of the evidence, then static fitness.

    This is the ordering the task asks for, and it is a weak one: the score
    ranks *failed runs*, so promoting the variants built from the least-bad
    failures is close to arbitrary.  It is kept explicit rather than quietly
    replaced by fitness, and the caveat is repeated in the report.
    """

    ranked = sorted(
        variants,
        key=lambda variant: (
            -variant.fitness.mean_progress_score,
            -variant.fitness.score,
            variant.variant_id,
        ),
    )
    return ranked[:k]


def evolve(
    diagnoses: Sequence[Diagnosis],
    *,
    generations: int = 3,
    population: int = 4,
    seed_layers: Mapping[str, LayerText] | None = None,
    diagnoses_per_op: int = 3,
    fill_per_layer: int = 1,
    parent_similarity_floor: float = MIN_PARENT_SIMILARITY,
) -> EvolutionResult:
    """Run ``generations`` of deterministic mutation.  No I/O, no model.

    Population is filled by a fixed plan rather than by sampling, so the same
    corpus yields the same population on every machine.  Generation 1 writes
    four variants from the seed; each later generation takes the top two
    parents by ``progress_score`` and mutates a layer they have not touched yet,
    which is what keeps the layer activation from collapsing onto ``LD``.

    ``fill_per_layer=0`` turns off the per-layer top-up and is what the
    similarity baseline in ``scripts/run_evolution.py`` uses.

    ``parent_similarity_floor`` is the drift rail: a candidate whose cosine to
    its parent (the seed for generation 1) is below the floor is dropped and
    recorded under ``rejected`` instead of being written.  On this corpus the
    rail never trips -- parent-child similarity is 0.96-0.99 -- so it is a
    guard for future operators, not an active filter.
    """

    corpus = tuple(sorted(diagnoses, key=lambda item: item.run_id))
    base = {layer: (seed_layers or SEED_LAYERS)[layer] for layer in LAYERS}
    pools = {layer: _pool(corpus, layer) for layer in LAYERS}
    results: list[GenerationResult] = []
    previous: tuple[Variant, ...] = ()

    for generation in range(1, generations + 1):
        variants: list[Variant] = []
        rejected: list[Rejection] = []
        seen: set[str] = {variant.digest for variant in previous}
        if generation == 1:
            for index in range(population):
                plan = tuple(GEN1_PLAN[index % len(GEN1_PLAN)])
                candidate = _build(
                    variant_id=f"g1-v{index}",
                    generation=1,
                    parent_id=None,
                    base=base,
                    plan=plan,
                    pool=pools,
                    offset=index,
                    count=diagnoses_per_op,
                    corpus=corpus,
                    fill_count=fill_per_layer,
                    stride=max(1, population),
                )
                ok, similarity, rejection = _gate(
                    candidate.variant_id, "seed", candidate.layers, base, parent_similarity_floor
                )
                if not ok:
                    assert rejection is not None
                    rejected.append(rejection)
                    continue
                variants.append(replace(candidate, parent_similarity=similarity))
        else:
            parents = _select_parents(previous, k=max(2, population // 2))
            if not parents:
                # Everything in the previous generation was rejected.  Say so
                # instead of silently re-deriving from the seed, which would
                # make the run look like it had a second generation.
                results.append(
                    GenerationResult(
                        generation=generation,
                        variants=(),
                        similarity=similarity_report(()),
                        rejected=tuple(rejected),
                        diversity=diversity_report((), base),
                    )
                )
                previous = ()
                continue
            for index in range(population):
                parent = parents[index % len(parents)]
                candidates = [layer for layer in LAYERS if layer not in parent.mutated_layers]
                if not candidates:
                    candidates = list(LAYERS)
                # Prefer the layer this parent has the least evidence for: the
                # point of a second generation is to cover what the first one
                # left thin, not to keep re-densifying the same layer.
                covered = {
                    layer: sum(1 for run_id in parent.evidence_run_ids if run_id in _ids(pools[layer]))
                    for layer in candidates
                }
                candidates.sort(key=lambda layer: (covered[layer], LAYERS.index(layer)))
                layer = candidates[(generation + index) % len(candidates)]
                operator = OP_CYCLE[(generation + index) % len(OP_CYCLE)]
                plan = ((layer, "densify"), (layer, operator))
                if plan[0][1] == plan[1][1]:
                    plan = ((layer, "densify"),)
                candidate = _build(
                    variant_id=f"g{generation}-v{index}",
                    generation=generation,
                    parent_id=parent.variant_id,
                    base=parent.layers,
                    plan=plan,
                    pool=pools,
                    offset=index,
                    count=2,
                    corpus=corpus,
                    fill_count=fill_per_layer,
                    stride=max(1, population),
                )
                if candidate.digest in seen:
                    # Deterministic fallback: stack one more densify on the
                    # next layer with a different slice, and say so in the id.
                    fallback_layer = LAYERS[(LAYERS.index(layer) + 1) % len(LAYERS)]
                    candidate = _build(
                        variant_id=f"g{generation}-v{index}b",
                        generation=generation,
                        parent_id=parent.variant_id,
                        base=parent.layers,
                        plan=((layer, "densify"), (layer, operator), (fallback_layer, "densify")),
                        pool=pools,
                        offset=index + population,
                        count=2,
                        corpus=corpus,
                        fill_count=fill_per_layer,
                        stride=max(1, population),
                    )
                ok, similarity, rejection = _gate(
                    candidate.variant_id,
                    parent.variant_id,
                    candidate.layers,
                    parent.layers,
                    parent_similarity_floor,
                )
                if not ok:
                    assert rejection is not None
                    rejected.append(rejection)
                    continue
                variants.append(replace(candidate, parent_similarity=similarity))
        # Deduplicate within the generation, keeping the first occurrence.  A
        # smaller generation is reported honestly rather than padding it.
        unique: list[Variant] = []
        used: set[str] = set()
        for variant in variants:
            if variant.digest in used:
                continue
            used.add(variant.digest)
            unique.append(variant)
        seen |= used
        results.append(
            GenerationResult(
                generation=generation,
                variants=tuple(unique),
                similarity=similarity_report(unique),
                rejected=tuple(rejected),
                diversity=diversity_report(unique, base),
            )
        )
        previous = tuple(unique)

    return EvolutionResult(
        seed_policy_version=policy_version(base, corpus),
        corpus_digest=corpus_digest(corpus),
        generations=tuple(results),
        diagnoses=corpus,
        parent_similarity_floor=parent_similarity_floor,
    )


def dumps(result: EvolutionResult) -> str:
    """Canonical JSON for the whole run -- byte-stable, used by the tests."""

    return json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)
