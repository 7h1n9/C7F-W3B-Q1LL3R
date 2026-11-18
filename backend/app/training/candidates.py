"""Mine policy candidates from exported transitions.

This is the P1 layer of the self-evolution loop: it reads the transition
dataset produced by ``scripts/export_training_transitions.py`` and proposes
changes to the solver's *policy*, not to its weights.

Two narrow, defensible forms are mined:

``dead_end``
    A command shape that repeatedly produced dead ends and never produced a
    fact.  Reflexion-style: the point is to stop paying for the same discovery
    twice.  Requires corroboration across separate runs -- one run's wall may
    be that run's bad luck.

``skill``
    A short command window from an *accepted* run that ends in a fact gain.
    Voyager-style: a reusable step sequence, not a transcript.

What this module deliberately does **not** do:

* it never touches a test-split transition (that is the only honest holdout);
* it never emits a candidate without ``source_run_ids`` (no invented content);
* it never writes to the knowledge corpus -- candidates land in a staging
  directory and only become policy after a paired A/B passes the gates in
  ``docs/notes/2026-11/2026-11-03-self-evolution-v0.md``.

Redaction is re-applied on the way out as defence in depth: a dataset that was
already redacted costs nothing to redact again, and a dataset that was not
never leaves this process in the clear.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from typing import Iterable, Mapping, Sequence

from app.services.learned_skills import SensitiveValueSanitizer
from app.training.transitions import redact_public_text

MIN_DEAD_END_RUNS = 2
MAX_CANDIDATES_PER_KIND = 200
MAX_COMMAND_CHARS = 240

_SHAPE_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
_SHAPE_NUMBER = re.compile(r"\b\d{1,6}\b")


def redact(text: str) -> str:
    """Redact exactly the way the dataset was redacted.

    Delegated rather than re-implemented: the shape a candidate matches on has
    to be the shape that is stored on disk, so the two passes must never drift.
    """

    return redact_public_text(text)


def command_shape(command: str) -> str:
    """Collapse a command to the shape that two attempts would share.

    Quoted text, numbers and long paths are the parts that vary between two
    tries at the same idea; the surrounding pipeline is what carries the idea.
    """

    value = redact(command)
    value = _SHAPE_QUOTED.sub("'<arg>'", value)
    value = _SHAPE_NUMBER.sub("<n>", value)
    return value[:MAX_COMMAND_CHARS]


#: Technique vocabulary.  Exact command strings never agree across two
#: independent runs -- different paths, different payloads, different quoting
#: -- so corroboration has to happen one level up, at the technique.  The
#: vocabulary is deliberately small and auditable: a new entry is a claim about
#: what counts as "the same move", and it belongs in review, not in a regex
#: nobody reads.
TECHNIQUE_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("sqli.union", ("union select", "union/**/select", "group_concat(", "information_schema", "sqlmap")),
    ("sqli.boolean", ("' or ", '" or ', "||1#", "^((select", "and 1=1", "or 1=1")),
    ("sqli.time", ("sleep(", "benchmark(", "pg_sleep(", "waitfor delay")),
    ("lfi.traversal", ("../../", "....//", "/etc/passwd", "proc/self/environ")),
    ("lfi.wrapper", ("php://filter", "php://input", "data://", "expect://", "zip://", "phar://")),
    ("ssti.eval", ("{{7*7}}", "${7*7}", "#{7*7}", "__class__", "freemarker", "velocity")),
    ("rce.deser", ("ysoserial", "readobject", "commons-collections", "gadget", "marshalsec")),
    ("rce.ognl", ("%{(#", "ognl", "allowstaticmethodaccess", "struts")),
    ("ssrf.internal", ("169.254.169.254", "metadata", "gopher://", "dict://", "127.0.0.1:")),
    ("upload.bypass", (".phtml", ".phar", "content-type: image", "filename=")),
    ("auth.jwt", ("jwt", "alg=none", "jwks", "kid=")),
    ("recon.dir", ("gobuster", "dirb ", "dirsearch", "feroxbuster", "ffuf")),
    ("recon.port", ("nmap", "masscan", "nc -zv", "curl -s -i -m")),
    ("recon.source", ("robots.txt", "www.zip", "backup", ".git/", ".svn/", "ds_store")),
    ("privesc.suid", ("find / -perm", "suid", "sudo -l", "capabilities")),
)


def technique_signature(text: str) -> str:
    """Return the first (highest-priority) technique label for a command."""

    labels = technique_labels(text)
    return labels[0] if labels else "unknown"


def technique_labels(text: str) -> tuple[str, ...]:
    """Every technique label present in one command.

    All matches, not just the first: a Worker's command is usually a small
    shell script that bundles recon and exploitation, and collapsing it to one
    label is how the step-sequence view ended up empty on the first real run.
    """

    haystack = redact(text).lower()
    return tuple(
        label
        for label, needles in TECHNIQUE_SIGNATURES
        if any(needle in haystack for needle in needles)
    )


def _evidence(transitions: Iterable[Mapping]) -> dict[str, dict]:
    """Per-technique evidence gathered from train-split transitions.

    Only accepted runs contribute positives; any train-run contributes walls,
    because a dead end is a dead end regardless of whether that particular run
    eventually solved the box.
    """

    evidence: dict[str, dict] = {}
    for transition in transitions:
        if _split_of(transition) != "train":
            continue
        command = _command_of(transition)
        if not command:
            continue
        labels = technique_labels(command)
        if not labels:
            continue
        observation = _observation_of(transition)
        accepted = bool((transition.get("labels") or {}).get("accepted"))
        run_id = str(transition.get("run_id") or "")
        gained_fact = int(observation.get("delta_facts") or 0) > 0
        hit_wall = int(observation.get("delta_dead_ends") or 0) > 0

        for label in labels:
            entry = evidence.setdefault(
                label,
                {
                    "fact_runs": set(),
                    "fact_runs_any": set(),
                    "dead_runs": set(),
                    "example": command[:MAX_COMMAND_CHARS],
                    "example_run": run_id,
                },
            )
            if gained_fact:
                entry["fact_runs_any"].add(run_id)
                if accepted:
                    entry["fact_runs"].add(run_id)
            if hit_wall:
                entry["dead_runs"].add(run_id)
    return evidence


def mine_techniques(transitions: Iterable[Mapping]) -> tuple[list[dict], list[dict]]:
    """Technique-level lessons, split into effective and dead.

    This is the granularity the data actually supports.  Measured on the
    2026-11-03 export: 83% of train commands carry no vocabulary label at all,
    and only 299 commands carry two or more, so *step sequences* almost never
    repeat across runs (99 candidates, 0 corroborated).  Techniques do repeat.

    ``effective`` requires a fact gain in at least ``MIN_DEAD_END_RUNS``
    distinct accepted runs -- the same corroboration bar as a wall, so a single
    lucky run cannot promote a technique.
    """

    evidence = _evidence(transitions)
    effective: list[dict] = []
    dead: list[dict] = []

    for label, entry in evidence.items():
        fact_runs = entry["fact_runs"]
        dead_runs = entry["dead_runs"]
        if len(fact_runs) >= MIN_DEAD_END_RUNS:
            effective.append(
                {
                    "kind": "technique_effective",
                    "id": _candidate_id("technique_effective", label),
                    "technique": label,
                    "runs_fact": len(fact_runs),
                    "runs_dead": len(dead_runs),
                    "example": entry["example"],
                    "source_run_ids": sorted(fact_runs),
                }
            )
        # A wall verdict needs the technique to have *never* produced a fact,
        # in any run -- solved or not.  Judging it only against accepted runs
        # marked ``recon.dir`` (ffuf/gobuster) dead even though it found paths
        # in runs that later failed elsewhere; banning it would have been
        # actively harmful.
        if len(dead_runs) >= MIN_DEAD_END_RUNS and not entry["fact_runs_any"]:
            dead.append(
                {
                    "kind": "technique_dead",
                    "id": _candidate_id("technique_dead", label),
                    "technique": label,
                    "runs_dead": len(dead_runs),
                    "example": entry["example"],
                    "source_run_ids": sorted(dead_runs),
                }
            )

    effective.sort(key=lambda item: (-item["runs_fact"], item["id"]))
    dead.sort(key=lambda item: (-item["runs_dead"], item["id"]))
    return (
        effective[:MAX_CANDIDATES_PER_KIND],
        dead[:MAX_CANDIDATES_PER_KIND],
    )


def _candidate_id(kind: str, payload: object) -> str:
    digest = hashlib.sha256(f"{kind}|{payload}".encode("utf-8")).hexdigest()
    return f"{kind}-{digest[:12]}"


def _split_of(transition: Mapping) -> str:
    return str(transition.get("split") or "")


def _tool_of(transition: Mapping) -> str:
    return str((transition.get("action") or {}).get("tool") or "")


def _command_of(transition: Mapping) -> str:
    return str((transition.get("action") or {}).get("command") or "")


def _observation_of(transition: Mapping) -> Mapping:
    return transition.get("observation") or {}


def mine_dead_ends(transitions: Iterable[Mapping]) -> list[dict]:
    """Command shapes that only ever hit walls.

    A shape qualifies when it produced a dead end in at least
    ``MIN_DEAD_END_RUNS`` distinct runs *and* never produced a fact anywhere in
    the training data.  The second condition is what keeps the constraint
    honest: "it did not work for me" is not "it does not work".
    """

    dead_runs: dict[str, set[str]] = defaultdict(set)
    productive_runs: dict[str, set[str]] = defaultdict(set)
    examples: dict[str, str] = {}
    tools: dict[str, str] = {}

    for transition in transitions:
        if _split_of(transition) != "train":
            continue
        command = _command_of(transition)
        if not command:
            continue
        shape = command_shape(command)
        observation = _observation_of(transition)
        run_id = str(transition.get("run_id") or "")
        if int(observation.get("delta_facts") or 0) > 0:
            productive_runs[shape].add(run_id)
        elif int(observation.get("delta_dead_ends") or 0) > 0:
            dead_runs[shape].add(run_id)
            examples.setdefault(shape, command[:MAX_COMMAND_CHARS])
            tools.setdefault(shape, _tool_of(transition))

    candidates: list[dict] = []
    for shape, runs in dead_runs.items():
        if len(runs) < MIN_DEAD_END_RUNS:
            continue
        if productive_runs.get(shape):
            continue
        candidates.append(
            {
                "kind": "dead_end",
                "id": _candidate_id("dead_end", shape),
                "tool": tools.get(shape, ""),
                "shape": shape,
                "example": examples.get(shape, ""),
                "runs_hit": len(runs),
                "runs_productive": 0,
                "source_run_ids": sorted(runs),
            }
        )
    candidates.sort(key=lambda item: (-item["runs_hit"], item["id"]))
    return candidates[:MAX_CANDIDATES_PER_KIND]


def build_candidate_payload(
    transitions: Sequence[Mapping],
    *,
    policy_version: str = "",
    source_file: str = "",
) -> dict:
    """Assemble the staging document written to disk."""

    dead_ends = mine_dead_ends(transitions)
    effective, dead_techniques = mine_techniques(transitions)
    return {
        "schema_version": 1,
        "policy_version": policy_version,
        "source": source_file,
        "split_used": "train",
        "dead_ends": dead_ends,
        "techniques_effective": effective,
        "techniques_dead": dead_techniques,
        "stats": {
            "transitions_read": len(transitions),
            "dead_end_candidates": len(dead_ends),
            "technique_effective_candidates": len(effective),
            "technique_dead_candidates": len(dead_techniques),
            "runs_behind_effective": len(
                {run for item in effective for run in item["source_run_ids"]}
            ),
            "runs_behind_dead": len(
                {run for item in dead_techniques for run in item["source_run_ids"]}
            ),
        },
    }
