"""The worker prior's character budget is a hard cap, not a hint.

``build_worker_prior`` reserves a slice of the budget for each of the two
lower-trust, self-produced blocks (distilled trajectories, cross-challenge
lessons) and gives the remainder to the curated corpus.  Each slice had a floor
of 200 characters, which is fine on its own but is not a *joint* budget: two
floors plus the separators joining the blocks can exceed the caller's cap.

Once the recall fix made the learned block non-empty for the test's query, a
400-character budget started returning 401 -- one character over.  The invariant
matters because the caller sizes this for a prompt that has to fit.
"""

from __future__ import annotations

from app.services.knowledge_context import _clamp_prior_to_budget

MARKER = "\n[learned prior truncated]"


def test_text_within_budget_is_returned_unchanged() -> None:
    assert _clamp_prior_to_budget("short prior", 100) == "short prior"
    assert _clamp_prior_to_budget("", 100) == ""
    assert _clamp_prior_to_budget("exactly-ten", 11) == "exactly-ten"


def test_oversized_text_is_clamped_to_the_cap() -> None:
    body = "x" * 500

    assert len(_clamp_prior_to_budget(body, 400)) == 400


def test_a_zero_budget_yields_nothing() -> None:
    """The caller asked for nothing; returning text would break their prompt."""

    assert _clamp_prior_to_budget("anything", 0) == ""
    assert _clamp_prior_to_budget("anything", -5) == ""


def test_a_truncated_block_keeps_its_marker() -> None:
    """The result must read as deliberately shortened, not cut off mid-word."""

    body = "y" * 380 + MARKER

    clamped = _clamp_prior_to_budget(body, 400)

    assert len(clamped) <= 400
    assert clamped.endswith(MARKER)


def test_a_marker_larger_than_the_budget_does_not_overflow() -> None:
    body = "z" * 300 + MARKER

    clamped = _clamp_prior_to_budget(body, 5)

    assert len(clamped) == 5
    assert MARKER not in clamped


def test_the_real_budget_assembles_within_the_cap(monkeypatch) -> None:
    """The end-to-end assertion the regression broke (401 > 400)."""

    from types import SimpleNamespace

    from app.services import knowledge_context

    monkeypatch.setenv("C7F_KNOWLEDGE_MODE", "on")
    monkeypatch.delenv("C7F_KNOWLEDGE_ROOT", raising=False)
    knowledge_context.clear_retriever_cache()

    challenge = SimpleNamespace(
        id="ch-1",
        name="BUU SQL COURSE 1",
        description="SQL injection",
        challenge_type="WEB_TARGET",
        metadata_json={},
    )

    for budget in (200, 400, 800, 1600, 4000):
        prior = knowledge_context.KnowledgeContextService.build_worker_prior(
            SimpleNamespace(hints_json={"knowledge_mode": "on"}),
            challenge,
            intent_goal=(
                "SQL injection and command injection and path traversal and IDOR "
                "and SSRF probes"
            ),
            max_chars=budget,
        )
        assert len(prior["prior"]) <= budget, f"budget {budget} was exceeded"
