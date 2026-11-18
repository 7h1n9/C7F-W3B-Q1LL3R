"""Attribute a failed run to one of four layers, with evidence, not vibes.

The four layers
---------------
CyberEvolver's framing, mapped onto what this project actually has:

======  ==================  =====================================================
Layer   Name                Concrete carrier in this repo
======  ==================  =====================================================
``LS``  strategy            ``AGENTS.md`` opening discipline / the prompt's plan
``LI``  interface           ``container_exec``, path and tool availability
``LD``  knowledge           ``playbooks/*.jsonl`` domain recipes
``LP``  perception          the fact channel and observation compression
======  ==================  =====================================================

Why rules and not a model
-------------------------
A diagnosis that cannot be re-derived is useless for a paper: the reader has
to be able to check one run by hand and get the same answer.  So every
judgement here is an explicit, ordered predicate over the trajectory, each one
carrying the event ids it fired on.  There is no model call, no sampling, and
no clock: ``diagnose_run`` is a pure function, and the same run always produces
the same ``Diagnosis``.

The cost of that choice is coverage -- a rule set only recognises the failure
shapes somebody thought of.  The alternative (ask a model to label the run)
buys coverage at the price of a label nobody can audit or reproduce, which is
worse for the purpose this artifact serves.

Picking a layer
---------------
Each detector collects evidence and a score.  The winner is the highest score,
broken by :data:`TIE_BREAK_ORDER`, which is not alphabetical but causal:

``LI > LP > LD > LS``

A broken interface poisons every observation downstream, so it outranks the
symptoms it produces.  A silent fact channel similarly outranks the "the agent
keeps repeating itself" reading, because repetition is what a blind agent does.
Knowledge comes next: missing recipes explain wasted enumeration, but they do
not explain absent output.  Strategy is the residual -- it wins when the run
looked technically fine and simply went the wrong way.

``progress_score`` is deliberately *not* part of layer selection.  See its
docstring for why it must never be read as a capability number.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from app.training.trajectory import (
    DEFAULT_WINDOW,
    TrajectoryEvent,
    extract_artifacts,
    normalise_events,
)

# ---------------------------------------------------------------------------
# Layer constants
# ---------------------------------------------------------------------------

LS = "LS"
LI = "LI"
LD = "LD"
LP = "LP"
LAYERS = (LS, LI, LD, LP)

TIE_BREAK_ORDER = (LI, LP, LD, LS)

LAYER_NAMES = {
    LS: "strategy",
    LI: "interface",
    LD: "knowledge",
    LP: "perception",
}

# ---------------------------------------------------------------------------
# Detection vocabulary
# ---------------------------------------------------------------------------

#: Interface failures, ranked by how unambiguous they are.  ``command not
#: found`` can only mean a missing tool; ``no such file`` can also mean the
#: agent guessed a wrong path, so it is weaker evidence and scored lower.
#:
#: Every pattern is anchored to a full line.  Unanchored matching was tried
#: first and fired on prose: a worker's own plan ("try reading a.jpeg with
#: traversal") and a PHP ``<b>Parse error</b>`` both contain "no such file" or
#: "syntax error" and were being counted as container failures.
INTERFACE_SIGNALS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "tool_missing",
        re.compile(r"(?im)^[^\n]{0,180}\b(?:command not found|exec format error)\b[^\n]*$"),
    ),
    (
        "path_missing",
        re.compile(
            r"(?im)^[^\n]{0,180}(?:no such file or directory"
            r"|cannot access '[^']*': no such|cannot open '[^']*': no such"
            r"|no such file)[^\n]*$"
        ),
    ),
    (
        "permission",
        re.compile(r"(?im)^[^\n]{0,180}(?:permission denied|operation not permitted)[^\n]*$"),
    ),
    (
        "not_a_dir",
        re.compile(r"(?im)^[^\n]{0,180}(?:not a directory|is a directory)[^\n]*$"),
    ),
    (
        "bad_option",
        re.compile(r"(?im)^[^\n]{0,160}(?:unrecognized arguments|unknown option|invalid option)[^\n]*$"),
    ),
    (
        "shell_syntax",
        re.compile(
            r"(?im)^[^\n]{0,160}(?:syntax error|unexpected token|unexpected EOF|"
            r"bad substitution)[^\n]*$"
        ),
    ),
)

#: A command that ships code to the *target* produces target-side errors.  A
#: ``cat: /flag: No such file`` printed by a php payload is a wrong guess about
#: the victim, not an interface failure on the worker, and must not be counted
#: as ``LI``.
REMOTE_PAYLOAD_RE = re.compile(
    r"(?i)--data|--data-urlencode|--data-binary|<\?php|php://|allow_url_include|"
    r"auto_prepend|\?-d\+|jndi:|union\s+select|<script"
)

#: Markers that only appear when the worker's *shell* ran something, not the
#: target: html line breaks around output, and ``sh: 1:`` from ``shell_exec``.
REMOTE_ECHO_RE = re.compile(r"(?i)<br\s*/?>|sh: \d+: |uid=\d+\([a-z]+\)")

#: Anything that addresses the target.  ``{{target_url}}`` / ``{{target_host}}``
#: are the export's redaction tokens and are how 99% of commands refer to the
#: box, so they count as target contact.
TARGET_RE = re.compile(
    r"\{\{target_url\}\}|\{\{target_host\}\}|https?://|\bcurl\b|\bwget\b|"
    r"\bnmap\b|\bnc\b|\bsqlmap\b"
)

#: Recon verbs that enumerate without committing to a hypothesis.
ENUM_RE = re.compile(
    r"(?i)^\s*(?:cd\s+\S+\s*&&\s*)?(ls|find|dirb|gobuster|ffuf|feroxbuster|nmap|"
    r"whatweb|nikto|wpscan|enum4linux|cat)\b"
)

#: Same-command detection is *exact* after whitespace collapse.  Two tempting
#: loosenings were tried and rejected: stripping quoted bodies collapsed every
#: ``/bin/bash -lc '…'`` command to five tokens, and masking digits turned
#: ``/d3afe/d8cf`` and ``/d91b/d8cf`` into the same "shape".  Both inflated the
#: repeat counter until 60% of the corpus looked like a knowledge failure.
_WHITESPACE = re.compile(r"\s+")

#: Fact texts are quoted into ``state.recent_facts``; a command that reuses a
#: token of one is "grounded" in the fact channel.
FACT_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{5,}")


# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """One piece of trajectory evidence, addressable by event id."""

    event_index: int
    event_id: str
    kind: str
    excerpt: str

    def cite(self) -> str:
        return f"event {self.event_id or f'#{self.event_index}'} ({self.kind})"


@dataclass(frozen=True)
class Diagnosis:
    layer: str
    root_cause: str
    counterfactual: str
    blocking_hypothesis: str
    progress_score: float
    confidence: float
    evidence: tuple[Evidence, ...]
    layer_scores: Mapping[str, float]
    features: Mapping[str, Any]
    run_id: str = ""

    @property
    def layer_name(self) -> str:
        return LAYER_NAMES.get(self.layer, self.layer)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "layer": self.layer,
            "layer_name": self.layer_name,
            "root_cause": self.root_cause,
            "counterfactual": self.counterfactual,
            "blocking_hypothesis": self.blocking_hypothesis,
            "progress_score": round(self.progress_score, 2),
            "confidence": round(self.confidence, 3),
            "layer_scores": {k: round(v, 2) for k, v in self.layer_scores.items()},
            "evidence": [
                {
                    "event_index": e.event_index,
                    "event_id": e.event_id,
                    "kind": e.kind,
                    "excerpt": e.excerpt,
                }
                for e in self.evidence
            ],
            "features": dict(self.features),
        }

    def explain(self) -> str:
        lines = [
            f"layer={self.layer} ({self.layer_name}) confidence={self.confidence:.2f}",
            "scores: " + ", ".join(f"{k}={v:.1f}" for k, v in self.layer_scores.items()),
            f"root_cause: {self.root_cause}",
            f"counterfactual: {self.counterfactual}",
            f"blocking_hypothesis: {self.blocking_hypothesis}",
            f"progress_score: {self.progress_score:.1f}",
        ]
        for item in self.evidence:
            lines.append(f"  - {item.cite()}: {item.excerpt}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------


def _normalise_command(command: str) -> str:
    return _WHITESPACE.sub(" ", command or "").strip()


def _fact_tokens(text: str) -> set[str]:
    return {t.lower() for t in FACT_TOKEN_RE.findall(text or "")}


#: How far apart two identical commands may be and still count as one attempt.
#: The dataset logs a "started" row and a "completed" row for the same call:
#: 12188 of 43568 adjacent rows share a command and 11674 of those have an
#: empty observation on one side.  Treating them as two attempts inflates the
#: repeat counters by an order of magnitude and would put almost every run in
#: the knowledge layer.
RETRY_GAP = 1


def _combine(keep: TrajectoryEvent, drop: TrajectoryEvent) -> TrajectoryEvent:
    """Merge a duplicate pair without losing what either half observed."""

    return TrajectoryEvent(
        index=keep.index,
        sequence=keep.sequence,
        tool=keep.tool or drop.tool,
        command=keep.command,
        result=keep.result if len(keep.result) >= len(drop.result) else drop.result,
        delta_facts=keep.delta_facts + drop.delta_facts,
        delta_dead_ends=keep.delta_dead_ends + drop.delta_dead_ends,
        facts_after=max(keep.facts_after, drop.facts_after),
        dead_ends_after=max(keep.dead_ends_after, drop.dead_ends_after),
        event_id=keep.event_id or drop.event_id,
        turn=keep.turn if keep.turn is not None else drop.turn,
    )


def merge_retries(events: Sequence[TrajectoryEvent]) -> tuple[TrajectoryEvent, ...]:
    """Collapse the dataset's started/completed duplicate rows.

    Only adjacent rows with an identical command *and* at least one empty or
    identical observation are merged -- a genuine re-issue (same command, two
    different observations) is exactly the behaviour the knowledge-layer rule
    is looking for, so it must survive.
    """

    merged: list[TrajectoryEvent] = []
    for event in events:
        if merged:
            previous = merged[-1]
            same_command = previous.command and previous.command == event.command
            if same_command and abs(previous.index - event.index) <= RETRY_GAP:
                left, right = previous.result.strip(), event.result.strip()
                if not left or not right or left == right:
                    merged[-1] = _combine(previous, event)
                    continue
        merged.append(event)
    return tuple(merged)


def extract_features(events: Iterable[Any]) -> dict[str, Any]:
    """Everything the rules below need, computed in one pass.

    Returned as a plain dict so the caller can print it next to the verdict --
    a diagnosis whose inputs are hidden is not auditable.
    """

    raw = normalise_events(events)
    ordered = merge_retries(raw)
    n = len(ordered)
    if n == 0:
        return {
            "n": 0,
            "raw_events": 0,
            "facts_total": 0,
            "dead_total": 0,
            "target_first": None,
            "target_ratio": 0.0,
            "empty_ratio": 0.0,
            "repeat_ratio": 0.0,
            "repeat_pairs": 0,
            "enum_ratio": 0.0,
            "interface_hits": {},
            "interface_total": 0,
            "marker_count": 0,
            "flag_count": 0,
            "fact_grounded": 0,
            "zero_fact_streak": 0,
            "fact_stop_ratio": 1.0,
            "known_urls": 0,
        }

    facts_total = max((event.facts_after for event in ordered), default=0)
    dead_total = max((event.dead_ends_after for event in ordered), default=0)

    seen_shapes: dict[str, int] = {}
    repeat_pairs = 0
    first_repeat: tuple[int, TrajectoryEvent] | None = None
    max_same = 1

    interface_hits: dict[str, list[int]] = {}
    interface_lines: dict[str, str] = {}
    interface_events = 0
    empty = 0
    enum = 0
    target_first: int | None = None
    target_events = 0
    marker_count = 0
    flag_count = 0
    fact_grounded = 0
    streak = 0
    zero_fact_streak = 0
    last_fact_position: int | None = None
    urls: dict[str, None] = {}

    for position, event in enumerate(ordered):
        command = event.command or ""
        result = event.result or ""
        combined = f"{command}\n{result}"

        if TARGET_RE.search(command):
            target_events += 1
            if target_first is None:
                target_first = event.index
        if ENUM_RE.search(command):
            enum += 1
        if len(result.strip()) < 20:
            empty += 1

        for artifact in extract_artifacts(combined, max_paths=2):
            if artifact.startswith("{{target_url}}") or artifact.startswith("http"):
                urls.setdefault(artifact, None)

        if "VERIFIED_FACT=" in combined or "{{flag_pattern}}" in combined:
            marker_count += 1
        if "{{flag_pattern}}" in combined:
            flag_count += 1

        remote = bool(REMOTE_PAYLOAD_RE.search(command)) or bool(REMOTE_ECHO_RE.search(result))
        for kind, pattern in INTERFACE_SIGNALS:
            match = pattern.search(result)
            if not match:
                continue
            # Target-side output is not an interface failure.  There is no
            # escape hatch for ``{{linux_path}}``: the redaction token appears
            # inside PHP warnings served by the target too, so trusting it
            # reintroduced the false positives this anchor was added to stop.
            if remote:
                continue
            interface_hits.setdefault(kind, []).append(event.index)
            interface_lines.setdefault(kind, _matching_line(result, match.start()))
            interface_events += 1
            break

        if event.delta_facts:
            last_fact_position = position
            streak = 0
        else:
            streak += 1
            zero_fact_streak = max(zero_fact_streak, streak)

        if event.delta_facts and position + 1 < n:
            tokens: set[str] = set()
            for text in _recent_fact_texts(ordered, event.index):
                tokens |= _fact_tokens(text)
            lookahead = ordered[position + 1: position + 11]
            if tokens and any(tokens & _fact_tokens(e.command) for e in lookahead):
                fact_grounded += 1

        shape = _normalise_command(command)
        if shape:
            count = seen_shapes.get(shape, 0)
            if count:
                repeat_pairs += 1
                max_same = max(max_same, count + 1)
                if first_repeat is None:
                    first_repeat = (event.index, event)
            seen_shapes[shape] = count + 1

    fact_stop_ratio = 1.0
    if last_fact_position is not None and n:
        fact_stop_ratio = (last_fact_position + 1) / n

    return {
        "n": n,
        "raw_events": len(raw),
        "facts_total": facts_total,
        "dead_total": dead_total,
        "target_first": target_first,
        "target_ratio": target_events / n,
        "empty_ratio": empty / n,
        "repeat_ratio": repeat_pairs / n,
        "repeat_pairs": repeat_pairs,
        "max_same_command": max_same,
        "enum_ratio": enum / n,
        "interface_hits": {k: len(v) for k, v in sorted(interface_hits.items())},
        "interface_first": {k: v[0] for k, v in sorted(interface_hits.items())},
        "interface_lines": dict(sorted(interface_lines.items())),
        "interface_total": interface_events,
        "marker_count": marker_count,
        "flag_count": flag_count,
        "fact_grounded": fact_grounded,
        "zero_fact_streak": zero_fact_streak,
        "fact_stop_ratio": fact_stop_ratio,
        "known_urls": len(urls),
        "first_repeat_index": first_repeat[0] if first_repeat else None,
    }


def _recent_fact_texts(ordered: Sequence[TrajectoryEvent], index: int) -> list[str]:
    """Fact texts visible to the agent *after* event ``index``.

    ``TrajectoryEvent`` does not carry ``state.recent_facts`` (it is a window
    over earlier commands, and the dataset's own snapshot is authoritative for
    the counters), so grounding is measured against the first lines of the
    observations that produced facts.  That is a weaker proxy and is reported
    as such in the feature dict.
    """

    texts: list[str] = []
    for event in ordered[max(0, index - 3): index + 1]:
        if event.delta_facts and event.result:
            texts.append(event.result[:400])
    return texts


# ---------------------------------------------------------------------------
# Rule sets, one per layer
# ---------------------------------------------------------------------------


def _matching_line(text: str, position: int) -> str:
    """The full line containing *position* -- what a reader will grep for."""

    start = text.rfind("\n", 0, position) + 1
    end = text.find("\n", position)
    if end == -1:
        end = len(text)
    return text[start:end].strip()


def _excerpt(text: str, limit: int = 160) -> str:
    value = _WHITESPACE.sub(" ", (text or "").strip())
    return value[:limit]


def _evidence(
    ordered: Sequence[TrajectoryEvent],
    index: int,
    kind: str,
    text: str = "",
    *,
    prefer_command: bool = False,
) -> Evidence:
    """Build an :class:`Evidence` pointing at event *index*.

    ``prefer_command`` is for rules about *what the agent did* (a repeated
    request); the default shows what it *saw*, which is what an interface or
    perception rule needs.
    """

    for event in ordered:
        if event.index == index:
            if text:
                body = text
            elif prefer_command:
                body = event.command or event.result
            else:
                body = event.result or event.command
            return Evidence(
                event_index=index,
                event_id=event.event_id,
                kind=kind,
                excerpt=_excerpt(body),
            )
    return Evidence(event_index=index, event_id="", kind=kind, excerpt=_excerpt(text))


def _score_interface(features: Mapping[str, Any], ordered: Sequence[TrajectoryEvent]) -> tuple[float, list[Evidence], list[str]]:
    score = 0.0
    notes: list[str] = []
    evidence: list[Evidence] = []
    hits: dict[str, int] = dict(features.get("interface_hits") or {})
    total = int(features.get("interface_total") or 0)
    n = int(features.get("n") or 0) or 1
    first: dict[str, int] = dict(features.get("interface_first") or {})

    lines: dict[str, str] = dict(features.get("interface_lines") or {})
    if hits.get("tool_missing"):
        score += 3.0
        notes.append(f"{hits['tool_missing']} 次 `command not found`（工具缺失）")
        evidence.append(
            _evidence(ordered, first["tool_missing"], "li:tool_missing", lines.get("tool_missing", ""))
        )
    if len(hits) >= 2 and total >= 4:
        score += 2.0
        notes.append(f"接口类错误覆盖 {len(hits)} 种、共 {total} 次")
    elif hits.get("path_missing") or hits.get("not_a_dir"):
        score += 1.5
        notes.append(f"路径类错误 {hits.get('path_missing', 0)} 次")
        key = "path_missing" if hits.get("path_missing") else "not_a_dir"
        evidence.append(
            _evidence(ordered, first.get(key, 0), "li:path", lines.get(key, ""))
        )
    elif hits.get("shell_syntax"):
        score += 1.0
        notes.append(f"shell 语法错误 {hits['shell_syntax']} 次")
        evidence.append(_evidence(ordered, first["shell_syntax"], "li:syntax", lines.get("shell_syntax", "")))
    if total / n >= 0.15 and total >= 4:
        score += 1.5
        notes.append(f"接口错误密度 {total / n:.0%}")

    # Scoped narrowly on purpose: 'permission denied' on the target is a wrong
    # guess, so on its own it is capped below any other layer's signal.
    if hits.get("permission") and not hits.get("tool_missing"):
        score += 0.5
        evidence.append(_evidence(ordered, first["permission"], "li:permission", lines.get("permission", "")))

    return score, evidence, notes


def _score_knowledge(features: Mapping[str, Any], ordered: Sequence[TrajectoryEvent]) -> tuple[float, list[Evidence], list[str]]:
    score = 0.0
    notes: list[str] = []
    evidence: list[Evidence] = []

    repeats = int(features.get("repeat_pairs") or 0)
    n = int(features.get("n") or 0) or 1
    facts = int(features.get("facts_total") or 0)
    dead = int(features.get("dead_total") or 0)
    enum_ratio = float(features.get("enum_ratio") or 0.0)

    if repeats >= 5 and repeats / n >= 0.2:
        # Repeats alone are not a knowledge failure: a worker restart replays
        # the same warm-up, and that is a harness artifact.  Repeats only count
        # as a *missing recipe* when they also bought nothing.
        score += 2.0
        notes.append(f"同类请求重复 {repeats} 次（占 {repeats / n:.0%}）")
        index = features.get("first_repeat_index")
        if isinstance(index, int):
            evidence.append(_evidence(ordered, index, "ld:repeat", prefer_command=True))
        if facts <= 1:
            score += 1.0
            notes.append("重复期间 facts≤1，重复没有换来任何知识")
        if dead >= 3:
            score += 1.0
    elif repeats >= 3:
        score += 0.5

    if dead >= 3:
        score += 1.0
        notes.append(f"累计 {dead} 条 dead end")
    if dead >= 5 and facts <= 1:
        score += 1.5
        notes.append("dead end 堆积且几乎没有 fact 产出")

    if enum_ratio >= 0.45 and facts <= 1:
        score += 2.0
        notes.append(f"{enum_ratio:.0%} 的命令是无目标枚举（ls/find/nmap…）")
        for event in ordered:
            if ENUM_RE.search(event.command or ""):
                evidence.append(_evidence(ordered, event.index, "ld:enum"))
                break

    if score > 0 and not any(e.kind == "ld:repeat" for e in evidence):
        for event in ordered:
            if event.delta_dead_ends:
                evidence.append(_evidence(ordered, event.index, "ld:dead_end"))
                break

    return score, evidence, notes


def _score_perception(features: Mapping[str, Any], ordered: Sequence[TrajectoryEvent]) -> tuple[float, list[Evidence], list[str]]:
    score = 0.0
    notes: list[str] = []
    evidence: list[Evidence] = []

    n = int(features.get("n") or 0)
    facts = int(features.get("facts_total") or 0)
    empty_ratio = float(features.get("empty_ratio") or 0.0)
    grounded = int(features.get("fact_grounded") or 0)
    markers = int(features.get("marker_count") or 0)

    if n >= 20 and facts == 0:
        score += 4.0
        notes.append(f"{n} 条命令全程 facts=0，fact 通道从未落地")
        # Point at the widest silent stretch rather than the first event: one
        # empty recon observation proves nothing.
        evidence.append(_evidence(ordered, _widest_silence(ordered), "lp:no_fact"))
    if n >= 30 and empty_ratio >= 0.55 and facts <= 1:
        score += 2.0
        notes.append(f"{empty_ratio:.0%} 的观测为空或近空，压缩后几乎无信息")
        evidence.append(_evidence(ordered, _widest_silence(ordered), "lp:empty_obs"))
    if n >= 30 and facts >= 3 and grounded == 0:
        score += 1.5
        notes.append("有 fact 落地但后续命令从未引用（fact 通道被忽略）")
        for event in ordered:
            if event.delta_facts:
                evidence.append(_evidence(ordered, event.index, "lp:fact_ignored"))
                break
    if markers == 0 and n >= 30:
        score += 0.5
        notes.append("全程没有 VERIFIED_FACT 写入")

    return score, evidence, notes


def _widest_silence(ordered: Sequence[TrajectoryEvent]) -> int:
    """Index of the middle of the longest measurable-but-empty stretch."""

    best_start, best_len = 0, 0
    start = 0
    for event in ordered:
        if event.result.strip():
            if event.index - start > best_len:
                best_start, best_len = start, event.index - start
            start = event.index + 1
    if len(ordered) - start > best_len:
        best_start, best_len = start, len(ordered) - start
    return min(best_start + best_len // 2, len(ordered) - 1)


def _score_strategy(features: Mapping[str, Any], ordered: Sequence[TrajectoryEvent]) -> tuple[float, list[Evidence], list[str]]:
    score = 0.0
    notes: list[str] = []
    evidence: list[Evidence] = []

    n = int(features.get("n") or 0) or 1
    target_first = features.get("target_first")
    stop_ratio = float(features.get("fact_stop_ratio") or 1.0)
    facts = int(features.get("facts_total") or 0)

    if target_first is None and n >= 10:
        score += 4.0
        notes.append(f"{n} 条命令从未真正指向目标")
        evidence.append(_evidence(ordered, 0, "ls:no_target"))
    elif isinstance(target_first, int) and target_first >= 8:
        score += 3.0
        notes.append(f"前 {target_first} 条命令没有碰目标（长时间暖场）")
        evidence.append(_evidence(ordered, 0, "ls:warmup", ordered[0].command))
        evidence.append(_evidence(ordered, target_first, "ls:first_contact", ordered[target_first].command))

    if n >= 30 and facts > 0 and stop_ratio <= 0.5:
        score += 2.0
        notes.append(f"最后一次 fact 落在第 {stop_ratio:.0%} 步，后段不再产出知识")
    if n >= 30 and facts == 0 and stop_ratio >= 0.99:
        # Overlaps LP by construction; kept low so the tie-break hands silent
        # runs to perception, where the missing channel is the actual fault.
        score += 1.0

    return score, evidence, notes


_SCORERS = {
    LI: _score_interface,
    LD: _score_knowledge,
    LP: _score_perception,
    LS: _score_strategy,
}


# ---------------------------------------------------------------------------
# Narrative assembly
# ---------------------------------------------------------------------------


def _first_url(ordered: Sequence[TrajectoryEvent]) -> str:
    for event in ordered:
        for artifact in extract_artifacts(event.command or ""):
            if artifact.startswith("{{target_url}}") or artifact.startswith("http"):
                return artifact
    return "{{target_url}}/"


def _last_command(ordered: Sequence[TrajectoryEvent]) -> str:
    for event in reversed(ordered):
        if (event.command or "").strip():
            return event.command.strip()
    return ""


def _cite(evidence: Sequence[Evidence]) -> str:
    if not evidence:
        return "无可用证据"
    item = evidence[0]
    return f"{item.cite()} 的 {item.excerpt or '（空观测）'}"


def _counterfactual(layer: str, features: Mapping[str, Any], ordered: Sequence[TrajectoryEvent]) -> str:
    url = _first_url(ordered)
    n = int(features.get("n") or 0)

    if layer == LI:
        return (
            "先做一次接口自检再继续攻击："
            "`ls -la {{linux_path}} 2>&1; command -v curl php python3; "
            "printf '%s' \"$PWD\"` —— 任何一条 not found / no such file 都先修环境，"
            "不要在同一条坏命令上重试"
        )
    if layer == LP:
        return (
            f"把观测拉回 fact 通道：先跑 `curl -s -o /dev/null -w '%{{http_code}}' '{url}'` "
            "确认最小观测可回传，再用 `python3 blackboard.py write-fact \"...\"` 把结论落地；"
            "若仍无 fact，检查 result_excerpt 是否在入口被截断"
        )
    if layer == LD:
        return (
            f"停止枚举，改读配方后只发一条判定性请求：`curl -sS -i -m 20 '{url}'` "
            "之后按 playbook 的下一步分支（每条分支一条命令），并把失败原因写进 DEADEND= "
            "以免下一轮重复同一族请求"
        )
    return (
        f"改写开局策略：首条命令直接打目标而不是本地暖场，"
        f"例如 `curl -sS -i -m 20 '{url}' | head -60`，"
        f"并把 {n} 步内未接触目标的命令视为策略失败"
    )


def _blocking_hypothesis(layer: str, features: Mapping[str, Any], ordered: Sequence[TrajectoryEvent]) -> str:
    if layer == LI:
        return "假设工作区路径与工具链本身是可用的（`{{linux_path}}` 存在且可写）—— 轨迹里没有任何一步真正验证过这一点"
    if layer == LP:
        return "假设命令确实执行过、只是观测没有回传到 fact 通道 —— 空观测既可能是压缩丢失，也可能是执行失败，两者尚未区分"
    if layer == LD:
        return "假设这条请求族已经覆盖了目标的攻击面 —— 实际上只有重复，没有变体（参数、方法、编码都未改过）"
    return "假设目标可达且不需要先行侦察 —— 轨迹从未用一条完整请求证明过目标在线，暖场阶段也没有产出可供决策的事实"


# ---------------------------------------------------------------------------
# Progress score
# ---------------------------------------------------------------------------


def progress_score(features: Mapping[str, Any]) -> float:
    """Heuristic 0-100 progress signal for *ranking candidates within a
    generation*.

    **This is not a capability metric.**  It must not be reported as a success
    rate, must not be used to extend a deadline, and must not be used to
    estimate solve rate.  A run scoring 80 here is not "80% likely to solve":
    the score is a monotone blend of four trajectory-local quantities (facts
    landed, target contact share, observation density, dead-end drag), all of
    which a run can inflate without getting closer to the flag.  The paper's
    own experience is the reason for this warning -- agents that emit more
    ``write-fact`` calls and more target-shaped commands score higher while
    solving at the same rate, i.e. the proxy is hackable.  Treat it as a
    tie-breaker between candidates from the same generation under the same
    model, nothing else.

    中文口径（论文里必须照抄这一条）：本分数**仅用于同代候选之间的排序**，
    **不得作为能力指标**、**不得用于延长 deadline**、**不得用于判解出率**。
    论文自认的 80 分 ≠ 80% 成功率，存在 reward hacking（多写 fact、多发命中
    目标形态的命令即可抬高分数）。
    """

    n = int(features.get("n") or 0)
    if n == 0:
        return 0.0

    facts = int(features.get("facts_total") or 0)
    dead = int(features.get("dead_total") or 0)
    target_ratio = float(features.get("target_ratio") or 0.0)
    empty_ratio = float(features.get("empty_ratio") or 0.0)
    repeat_ratio = float(features.get("repeat_ratio") or 0.0)

    # Twelve facts is the practical ceiling of a strong run in this corpus;
    # log1p keeps the first few facts worth more than the tenth.
    info = min(1.0, math.log1p(facts) / math.log1p(12))
    focus = min(1.0, target_ratio / 0.6)
    evidence = 1.0 - min(1.0, empty_ratio)
    drag = min(1.0, dead / 8.0)

    raw = 0.45 * info + 0.30 * focus + 0.15 * evidence - 0.20 * drag
    raw -= 0.10 * min(1.0, repeat_ratio / 0.4)
    return max(0.0, min(100.0, 100.0 * raw))


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def diagnose_run(
    trajectory: Any,
    *,
    window: int = DEFAULT_WINDOW,
) -> Diagnosis:
    """Diagnose one run.

    ``trajectory`` is a :class:`~app.training.trajectory.RunTrajectory`, a list
    of dataset transition rows, or a list of ``TrajectoryEvent``.  ``window``
    is accepted for symmetry with ``compress_trajectory`` and deliberately
    unused: layer attribution reads events, not segments, so that the verdict
    cannot change with the compression window.
    """

    del window  # see docstring

    ordered = normalise_events(trajectory)
    features = extract_features(ordered)
    run_id = str(getattr(trajectory, "run_id", "") or "")

    if not ordered:
        return Diagnosis(
            layer=LS,
            root_cause="轨迹为空，无法归因",
            counterfactual="先补齐该 run 的事件导出，再重新诊断",
            blocking_hypothesis="未知：没有观测",
            progress_score=0.0,
            confidence=0.0,
            evidence=(),
            layer_scores={layer: 0.0 for layer in LAYERS},
            features=features,
            run_id=run_id,
        )

    scores: dict[str, float] = {}
    evidence_by_layer: dict[str, list[Evidence]] = {}
    notes_by_layer: dict[str, list[str]] = {}
    for layer in LAYERS:
        score, evidence, notes = _SCORERS[layer](features, ordered)
        scores[layer] = score
        evidence_by_layer[layer] = evidence
        notes_by_layer[layer] = notes

    best = max(scores.values())
    if best <= 0.0:
        # No rule fires: say so instead of inventing a cause.  Strategy is the
        # residual owner, but confidence is zero so a sweep can filter it out.
        layer = LS
        confidence = 0.0
        root_cause = (
            f"没有命中任何失败规则（{features['n']} 条命令，facts={features['facts_total']}，"
            f"重复率={features['repeat_ratio']:.0%}）：该 run 大概率不是失败样本，"
            "或失败形态超出当前规则集"
        )
    else:
        layer = next(candidate for candidate in TIE_BREAK_ORDER if scores[candidate] == best)
        total = sum(scores.values()) or 1.0
        confidence = round(min(1.0, best / total + 0.15 * min(1.0, best / 6.0)), 3)
        root_cause = f"{LAYER_NAMES[layer]}层：{'；'.join(notes_by_layer[layer])}。证据：" + _cite(evidence_by_layer[layer])

    return Diagnosis(
        layer=layer,
        root_cause=root_cause,
        counterfactual=_counterfactual(layer, features, ordered),
        blocking_hypothesis=_blocking_hypothesis(layer, features, ordered),
        progress_score=progress_score(features),
        confidence=confidence,
        evidence=tuple(evidence_by_layer[layer]),
        layer_scores=scores,
        features=features,
        run_id=run_id,
    )


def diagnose_runs(trajectories: Iterable[Any], *, window: int = DEFAULT_WINDOW) -> list[Diagnosis]:
    """Batch entry point; order follows the input."""

    return [diagnose_run(item, window=window) for item in trajectories]


def layer_distribution(diagnoses: Iterable[Diagnosis]) -> dict[str, int]:
    """Counts per layer, with every layer present (including zeros)."""

    counts = {layer: 0 for layer in LAYERS}
    for diagnosis in diagnoses:
        counts[diagnosis.layer] = counts.get(diagnosis.layer, 0) + 1
    return counts
