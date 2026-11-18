"""Flag-harvest coverage: a recovered flag must reach the official gate.

Run ``d3ac62f0`` recovered the Sentinel flag and even recorded it in its own
Blackboard (``FOUND_FLAG=CTF2{d63ea4dc-...}``), yet the Run finished
``COMPLETED_UNSOLVED`` because no platform code ever read a Worker's prose
claim.  The fix has three halves and each one gets a test here:

1. the challenge banner travels to every gate instead of being hardcoded;
2. a claim becomes a ``flag_found`` **only** through the official provenance
   check (verbatim in the recorded output + challenge format + not a template);
3. the flag hint is no longer tied to one tool name or to a typed handoff.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.schemas.challenge import flag_head_of, normalize_flag_pattern
from app.solver.muteki.adapter.upstream_runtime_graph import UpstreamRuntimeGraph
from app.solver.muteki.gate import MutekiFlagGate
from app.solver.muteki.worker.official_worker import (
    _build_prompt,
    _flag_gate_instruction,
)

_CHALLENGE_PATTERN = r"CTF2\{[^}]+\}"


def _challenge(pattern: str = _CHALLENGE_PATTERN) -> SimpleNamespace:
    return SimpleNamespace(
        id="flag-harvest",
        name="Flag harvest",
        challenge_type="WEB_TARGET",
        description="",
        target_url="http://target.test",
        flag_pattern=pattern,
    )


def _upstream(
    tmp_path,
    name: str,
    *,
    pattern: str = _CHALLENGE_PATTERN,
) -> UpstreamRuntimeGraph:
    return UpstreamRuntimeGraph(
        tmp_path / f"{name}.sqlite",
        challenge=_challenge(pattern),
        challenge_id=name,
    )


# ── the banner travels with the challenge ────────────────────────────────


def test_bare_banner_expands_to_the_full_pattern() -> None:
    assert normalize_flag_pattern("CTF2") == r"CTF2\{[^}]+\}"
    assert normalize_flag_pattern("flag") == r"flag\{[^}]+\}"
    assert normalize_flag_pattern("  DASCTF  ") == r"DASCTF\{[^}]+\}"


def test_a_full_expression_is_left_untouched() -> None:
    """An operator who wrote a real regex keeps it, verbatim."""

    assert normalize_flag_pattern(r"CTF2\{[^}]+\}") == r"CTF2\{[^}]+\}"
    assert normalize_flag_pattern(r"(?i)flag\{[^}]+\}") == r"(?i)flag\{[^}]+\}"


def test_an_empty_value_falls_back_to_the_default_shape() -> None:
    assert normalize_flag_pattern(None) == r"flag\{[^}]+\}"
    assert normalize_flag_pattern("   ") == r"flag\{[^}]+\}"


def test_the_bare_banner_can_be_recovered_for_editing() -> None:
    """Editing must show ``CTF2`` again, not the expanded boilerplate."""

    assert flag_head_of(r"CTF2\{[^}]+\}") == "CTF2"
    assert flag_head_of(r"flag\{[^}]+\}") == "flag"
    # A hand-written expression is not a banner: show it as-is.
    assert flag_head_of(r"(?i)flag\{[^}]+\}") == r"(?i)flag\{[^}]+\}"


def test_gate_honours_the_challenge_banner() -> None:
    """Hardcoding ``flag{...}`` rejected every challenge without that banner."""

    gate = MutekiFlagGate(_CHALLENGE_PATTERN)
    assert gate.verify("CTF2{abc}", real_output="prefix CTF2{abc} suffix").accepted is True
    assert gate.verify("flag{abc}", real_output="flag{abc}").accepted is False


def test_an_unusable_pattern_degrades_to_the_default_gate() -> None:
    gate = MutekiFlagGate("CTF2{[")

    assert gate.verify("flag{abc}", real_output="flag{abc}").accepted is True


# ── a claim becomes a flag only through the gate ─────────────────────────


def test_a_worker_claim_becomes_a_gated_flag(tmp_path) -> None:
    graph = _upstream(tmp_path, "harvest-claim")
    try:
        graph.add_native_fact(
            actor="claude",
            content="[claude] FOUND_FLAG=CTF2{d63ea4dc-b6e0-4cd4-ba47-15c1a3514623}",
        )

        flags = graph.flags(verified_only=True)
        assert [item.flag_value for item in flags] == [
            "CTF2{d63ea4dc-b6e0-4cd4-ba47-15c1a3514623}"
        ]
    finally:
        graph.close()


def test_a_prose_claim_without_the_marker_is_still_harvested(tmp_path) -> None:
    """The run also recorded ``Flag recovered ... : CTF2{...}`` without a marker."""

    graph = _upstream(tmp_path, "harvest-prose")
    try:
        graph.add_native_fact(
            actor="claude",
            content='Flag recovered via POST /submit (status "ok"): CTF2{aaaabbbbccccdddd}',
        )

        assert [item.flag_value for item in graph.flags(verified_only=True)] == [
            "CTF2{aaaabbbbccccdddd}"
        ]
    finally:
        graph.close()


def test_a_claim_that_does_not_match_the_challenge_banner_is_ignored(tmp_path) -> None:
    graph = _upstream(tmp_path, "harvest-mismatch")
    try:
        graph.add_native_fact(actor="claude", content="FOUND_FLAG=flag{aaaabbbbcccc}")

        assert graph.flags(verified_only=True) == []
    finally:
        graph.close()


def test_a_template_candidate_is_rejected_by_the_gate(tmp_path) -> None:
    """``CTF2{...}`` matches the banner but is a template, not a recovery."""

    graph = _upstream(tmp_path, "harvest-template")
    try:
        graph.add_native_fact(actor="claude", content="the flag looks like CTF2{...} here")

        assert graph.flags(verified_only=True) == []
    finally:
        graph.close()


def test_an_ordinary_fact_never_becomes_a_flag(tmp_path) -> None:
    graph = _upstream(tmp_path, "harvest-plain")
    try:
        graph.add_native_fact(actor="claude", content="the submit endpoint returns HTTP 200")
        graph.add_dead_end(actor="claude", description="FOUND_FLAG=CTF2{deadbeefdeadbeef}")

        assert graph.flags(verified_only=True) == []
    finally:
        graph.close()


def test_harvest_is_idempotent(tmp_path) -> None:
    """A second synchronisation pass must not duplicate the gate decision."""

    graph = _upstream(tmp_path, "harvest-idempotent")
    try:
        graph.add_native_fact(actor="claude", content="FOUND_FLAG=CTF2{aaaabbbbccccdddd}")
        first = [(item.flag_id, item.flag_value) for item in graph.flags(verified_only=True)]

        graph.add_native_fact(actor="claude", content="acknowledged the recovered flag")
        second = [(item.flag_id, item.flag_value) for item in graph.flags(verified_only=True)]

        assert first == second
        assert len(first) == 1
    finally:
        graph.close()


# ── the hint is tool-agnostic ────────────────────────────────────────────


def test_the_flag_hint_is_not_limited_to_one_tool() -> None:
    """A model-inversion oracle is not ``boolean_config_extract``."""

    assert _flag_gate_instruction({}) != ""
    assert _flag_gate_instruction({"tool_name": "model_inversion_probe"}) != ""


def test_the_hint_names_the_challenge_format() -> None:
    instruction = _flag_gate_instruction({}, _CHALLENGE_PATTERN)

    assert _CHALLENGE_PATTERN in instruction
    assert "write-flag" in instruction


def test_the_prompt_offers_the_flag_handoff_without_a_typed_handoff() -> None:
    """Exploration intents (no typed contract) still need the handoff."""

    prompt = _build_prompt("probe the oracle", "intent-9", {}, _CHALLENGE_PATTERN)

    assert "write-flag" in prompt
    assert _CHALLENGE_PATTERN in prompt


# ── the producing side's verdict is not trusted ──────────────────────────
#
# Run ``49af30bf`` (challenge ``xianzhi_xss``, declared banner ``CTF2``) recorded
# ``flag{moxiaoxi}`` -- the flag of the ORIGINAL public challenge -- as
# ``flag_found verified=1`` and finished ``COMPLETED_SOLVED``.  The platform
# rejected the submission.  The official graph marks every ``flag_found`` it
# holds as verified, so the host has to re-apply the declared banner itself.


def _write_raw_flag(graph: UpstreamRuntimeGraph, flag: str) -> None:
    """Write a ``flag_found`` the way the container-side worker does.

    Bypasses ``add_native_fact`` on purpose: that path runs the host gate, and
    the defect being pinned here is precisely that the container's own write
    never consulted it.
    """

    graph.native_graph().flag_found(actor="worker-3", flag=flag)


def test_a_flag_that_does_not_fit_the_banner_is_not_a_solve(tmp_path) -> None:
    graph = _upstream(tmp_path, "wrong-banner")
    try:
        _write_raw_flag(graph, "flag{moxiaoxi}")

        assert graph.flags(verified_only=True) == [], "a wrong-banner value is not a solve"
        assert [item.flag_value for item in graph.suspected_flags()] == ["flag{moxiaoxi}"]
    finally:
        graph.close()


def test_a_flag_that_fits_the_banner_is_still_a_solve(tmp_path) -> None:
    """The banner check must not become a way to lose a real flag."""

    graph = _upstream(tmp_path, "right-banner")
    try:
        _write_raw_flag(graph, "CTF2{9a68ffd9-8397-4634-8bd4-fa695c44be5b}")

        assert [item.flag_value for item in graph.flags(verified_only=True)] == [
            "CTF2{9a68ffd9-8397-4634-8bd4-fa695c44be5b}"
        ]
        assert graph.suspected_flags() == []
    finally:
        graph.close()


def test_both_kinds_are_separated_when_they_coexist(tmp_path) -> None:
    graph = _upstream(tmp_path, "mixed-banners")
    try:
        _write_raw_flag(graph, "flag{moxiaoxi}")
        _write_raw_flag(graph, "CTF2{real-one}")

        assert [item.flag_value for item in graph.flags(verified_only=True)] == ["CTF2{real-one}"]
        assert [item.flag_value for item in graph.suspected_flags()] == ["flag{moxiaoxi}"]
    finally:
        graph.close()


def test_a_token_challenge_declares_no_shape_to_check(tmp_path) -> None:
    """``flag_format="token"`` means "any bare token"; nothing may be held."""

    graph = _upstream(tmp_path, "token-mode", pattern="token")
    try:
        _write_raw_flag(graph, "any-token-at-all")

        assert [item.flag_value for item in graph.flags(verified_only=True)] == [
            "any-token-at-all"
        ]
        assert graph.suspected_flags() == []
    finally:
        graph.close()
