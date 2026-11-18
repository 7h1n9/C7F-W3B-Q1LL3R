"""Recovery of a flag from evidence when only the *value* was captured.

These lock the three properties that make the post-timeout submission safe to
run unattended:

1. A value the box produced is accepted under the challenge's own banner.
2. A value absent from the evidence is refused and demoted to review, never
   promoted.
3. Ordinary ``verify`` still requires the whole string in the output, so the
   new path cannot be used to relax the gate for a normal Worker claim.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.solver.muteki.gate import MutekiFlagGate
from app.solver.muteki.graph import MutekiGraph
from app.solver.muteki.recovery import (
    banner_of,
    candidates_from_text,
    wrap_with_banner,
)

PATTERN = r"CTF2\{[^}]+\}"


def _graph(pattern: str = PATTERN) -> MutekiGraph:
    path = Path(tempfile.mkdtemp()) / "graph.db"
    return MutekiGraph(path, challenge_id="recovery", flag_pattern=pattern)


def test_banner_is_read_from_the_challenge_pattern() -> None:
    """The arena banner is the half of a flag the platform tells us up front."""

    assert banner_of(PATTERN) == "CTF2"
    assert banner_of(r"flag\{[^}]+\}") == "flag"
    assert banner_of(r"DASCTF\{[^}]+\}") == "DASCTF"
    # A generic or unusable pattern falls back rather than crashing the path.
    assert banner_of("") == "flag"
    assert banner_of(None) == "flag"


def test_the_value_survives_a_wrong_banner() -> None:
    """A box that wraps its flag in its own banner still yields a usable value."""

    found = candidates_from_text("FOUND_FLAG=n1book{afr_1_solved}", source="fact:1")
    assert [item.value for item in found] == ["afr_1_solved"]
    assert found[0].banner == "n1book"

    spellings = wrap_with_banner(found[0], PATTERN)
    # The challenge's banner comes first: it is the only one the gate can accept.
    assert spellings[0] == "CTF2{afr_1_solved}"
    # The original wrapper is kept as a second chance.
    assert "n1book{afr_1_solved}" in spellings


def test_a_bare_labelled_value_is_a_candidate() -> None:
    """Some Workers report the value with no wrapper at all."""

    found = candidates_from_text("FOUND_FLAG=abc123def", source="fact:2")
    assert [(item.value, item.banner) for item in found] == [("abc123def", "")]


def test_prose_braces_are_not_candidates() -> None:
    """CSS, JSON and template braces must not become flag shapes."""

    assert candidates_from_text("nothing here {css} and {}") == []
    assert candidates_from_text("") == []


def test_a_witnessed_value_is_accepted_under_the_challenge_banner() -> None:
    """The value appeared in the output; only the banner is ours to supply."""

    graph = _graph()
    evidence = "FOUND_FLAG=n1book{afr_1_solved}"
    decision = graph.gate.verify_recovered(
        "CTF2{afr_1_solved}",
        real_output=evidence,
        recovered_value="afr_1_solved",
    )
    assert decision.accepted
    assert decision.reason == "ACCEPTED_RECOVERED"

    graph.record_flag_decision(actor="coordinator:recovery", decision=decision, evidence=evidence)
    assert [str(flag.flag_value) for flag in graph.flags(verified_only=True)] == [
        "CTF2{afr_1_solved}"
    ]


def test_an_unwitnessed_value_is_refused_and_kept_for_review() -> None:
    """No evidence means no solve -- and the claim still stays visible."""

    graph = _graph()
    decision = graph.gate.verify_recovered(
        "CTF2{afr_1_solved}",
        real_output="id: www-data",
        recovered_value="afr_1_solved",
    )
    assert not decision.accepted
    assert decision.reason == "NOT_IN_REAL_OUTPUT"

    graph.record_flag_decision(actor="coordinator:recovery", decision=decision, evidence="id")
    assert graph.flags(verified_only=True) == []
    assert [str(flag.flag_value) for flag in graph.suspected_flags()] == ["CTF2{afr_1_solved}"]


def test_the_ordinary_gate_still_demands_the_whole_string() -> None:
    """``verify_recovered`` must not become a way to weaken normal claims."""

    gate = MutekiFlagGate(PATTERN)
    # The value is present, but the full candidate is not: an ordinary claim is
    # still refused, because a Worker is expected to have *read* the banner.
    assert not gate.verify(
        "CTF2{afr_1_solved}", real_output="n1book{afr_1_solved}"
    ).accepted
    # And a placeholder is refused on both paths.
    assert not gate.verify_recovered(
        "CTF2{test}", real_output="n1book{test}", recovered_value="test"
    ).accepted


def test_a_value_that_cannot_be_expressed_is_refused() -> None:
    """The pattern still rules: a value that breaks it is not a candidate."""

    graph = _graph(r"flag\{[0-9a-f]{32}\}")
    decision = graph.gate.verify_recovered(
        "CTF2{afr_1_solved}",
        real_output="n1book{afr_1_solved}",
        recovered_value="afr_1_solved",
    )
    assert not decision.accepted
    assert decision.reason == "FORMAT_INVALID"


# ── a recovered guess must never read as a Worker's finding (2026-10-25) ────


def test_a_recovered_wrapper_is_not_a_flag() -> None:
    """Measured on ``bestphp's revenge`` (run ``07d44a1b``), and it cost 2.9 hours.

    ``Coordinator._offer_recovered_flag`` takes a value the box printed under its
    own banner, re-wraps it in the challenge's declared banner, and records it as
    a candidate.  That is a guess about the wrapper -- its docstring says so, and
    it returns ``False`` precisely so it never declares a solve.

    What happened instead: the re-wrapped ``CTF2{<uuid>}`` matched the declared
    ``CTF2\\{[^}]+\\}``, so ``flags()`` returned it, ``flag_found`` became True,
    ``report_json.flag_verified`` became True,
    ``materialize_completion_gate_flag`` filed it ``VALID``, and the Run closed
    ``COMPLETED_SOLVED`` while the platform rejected all five spellings.  The
    queue item stayed UNSOLVED, so the two ledgers disagreed.

    Format matching cannot separate these -- producing a matching format is the
    entire purpose of re-wrapping.  Provenance can, and ``actor`` carries it.
    """

    from app.solver.muteki.adapter.upstream_runtime_graph import UpstreamRuntimeGraph

    assert UpstreamRuntimeGraph._is_recovered_guess("coordinator:recovery:CTF2")
    assert UpstreamRuntimeGraph._is_recovered_guess("coordinator:recovery:bare")
    assert UpstreamRuntimeGraph._is_recovered_guess("Coordinator:Recovery:flag")

    # A Worker's own finding must never be caught by this filter.
    for actor in ("worker-1", "worker-2", "claude", "codex", "upstream", ""):
        assert not UpstreamRuntimeGraph._is_recovered_guess(actor), actor


def test_the_recovered_value_still_reaches_review() -> None:
    """Excluding it from ``flags()`` must not delete the evidence.

    ``suspected_flags()`` is the only surface a human can promote from.  A value
    dropped by both lists would leave no record that it was ever tried -- the
    exact failure ``record_flag_decision`` was written to prevent.
    """

    from app.solver.muteki.adapter.upstream_runtime_graph import UpstreamRuntimeGraph

    def is_suspected(actor: str, flag: str, declared: str = r"CTF2\{[^}]+\}") -> bool:
        import re

        matches = bool(re.fullmatch(declared, flag))
        return bool(flag) and (
            UpstreamRuntimeGraph._is_recovered_guess(actor) or not matches
        )

    # A recovered value whose format happens to match: suspected, not a flag.
    assert is_suspected("coordinator:recovery:CTF2", "CTF2{abc}")
    # A recovered value with a foreign banner: still suspected.
    assert is_suspected("coordinator:recovery:LCTF", "LCTF{abc}")
    # A Worker's wrong-banner value: suspected (unchanged behaviour).
    assert is_suspected("worker-1", "hitcon{abc}")
    # A Worker's matching value: NOT suspected -- it is a real candidate.
    assert not is_suspected("worker-1", "CTF2{abc}")
