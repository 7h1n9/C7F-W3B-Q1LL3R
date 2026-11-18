"""The upstream adapter must keep a refused flag value visible.

``write_flag`` can only accept: a value the gate refuses becomes a dead end, so a
Worker that printed the right value under the wrong banner used to leave no trace.
This adapter is what production actually runs (``runtime_graph_backend`` defaults
to ``upstream``), so the recovery and harvest paths only work if the candidate
write exists *here* -- a ``MutekiGraph``-only implementation would pass every
in-process test and do nothing in a real Run.
"""

from __future__ import annotations

from app.solver.muteki.adapter.upstream_runtime_graph import UpstreamRuntimeGraph
from app.solver.muteki.coordinator import _RefusedCandidate


def _graph(tmp_path, pattern: str) -> UpstreamRuntimeGraph:
    return UpstreamRuntimeGraph(
        tmp_path / "graph.sqlite",
        challenge=type(
            "Challenge",
            (),
            {
                "id": "c",
                "name": "c",
                "challenge_type": "WEB_TARGET",
                "description": "",
                "target_url": "http://target.test",
                "flag_pattern": pattern,
            },
        )(),
        challenge_id="run",
    )


def test_a_foreign_banner_lands_as_a_suspected_flag(tmp_path) -> None:
    graph = _graph(tmp_path, r"CTF2\{[^}]+\}")
    try:
        graph.record_flag_decision(
            actor="coordinator:candidate",
            decision=_RefusedCandidate("n1book{afr_1_solved}"),
            evidence="//n1book{afr_1_solved}",
        )
        # Not a solve ...
        assert graph.flags(verified_only=True) == []
        # ... but visible for review.
        assert [str(f.flag_value) for f in graph.suspected_flags()] == [
            "n1book{afr_1_solved}"
        ]
    finally:
        graph.close()


def test_a_matching_banner_becomes_a_verified_flag(tmp_path) -> None:
    graph = _graph(tmp_path, r"CTF2\{[^}]+\}")
    try:
        graph.record_flag_decision(
            actor="coordinator:recovery",
            decision=_RefusedCandidate("CTF2{afr_1_solved}"),
            evidence="recovered",
        )
        assert [str(f.flag_value) for f in graph.flags(verified_only=True)] == [
            "CTF2{afr_1_solved}"
        ]
        assert graph.suspected_flags() == []
    finally:
        graph.close()


def test_both_spellings_can_coexist(tmp_path) -> None:
    """What the harvest stores and what the recovery tries are different strings."""

    graph = _graph(tmp_path, r"CTF2\{[^}]+\}")
    try:
        graph.record_flag_decision(
            actor="coordinator:harvest",
            decision=_RefusedCandidate("n1book{afr_1_solved}"),
            evidence="as found",
        )
        graph.record_flag_decision(
            actor="coordinator:recovery",
            decision=_RefusedCandidate("CTF2{afr_1_solved}"),
            evidence="re-wrapped",
        )
        assert [str(f.flag_value) for f in graph.flags(verified_only=True)] == [
            "CTF2{afr_1_solved}"
        ]
        assert [str(f.flag_value) for f in graph.suspected_flags()] == [
            "n1book{afr_1_solved}"
        ]
    finally:
        graph.close()


def test_an_empty_value_is_ignored(tmp_path) -> None:
    graph = _graph(tmp_path, r"CTF2\{[^}]+\}")
    try:
        assert graph.record_flag_decision(
            actor="x", decision=_RefusedCandidate(""), evidence=""
        ) == -1
        assert graph.flags(verified_only=True) == []
        assert graph.suspected_flags() == []
    finally:
        graph.close()
