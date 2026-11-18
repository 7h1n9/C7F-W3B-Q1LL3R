"""Paired inference for the A/B experiment (CAP-03).

The summary used to refuse to say anything beyond a mean delta, which is the
honest position when the sample is tiny but not a useful one when it is not.
These tests pin both halves: the exact test's arithmetic, and the fact that it
stays withheld while the sample is below the floor.
"""

from __future__ import annotations

import pytest

from app.benchmark.experiment import (
    BOOTSTRAP_SEED,
    MIN_PAIRED_SAMPLES,
    _bootstrap_mean_ci,
    _exact_mcnemar_p,
    build_summary,
)


def _record(arm: str, repeat: int, success: bool, **report_extra) -> dict:
    return {
        "case_id": "case-sqli",
        "arm": arm,
        "repeat": repeat,
        "status": "COMPLETED",
        "success": success,
        "error": None,
        "evaluation_report": {
            "final_result": "SUCCESS" if success else "INCOMPLETE",
            **report_extra,
        },
    }


def _pairs(count: int, *, off_until: int, on_until: int, **report_extra) -> list[dict]:
    records: list[dict] = []
    for repeat in range(1, count + 1):
        for arm, success in (("off", repeat <= off_until), ("on", repeat <= on_until)):
            records.append(_record(arm, repeat, success, **report_extra))
    return records


def test_exact_mcnemar_matches_the_binomial_tail():
    # b=4, c=0 -> 2 * C(4,0)/2^4 = 0.125
    assert _exact_mcnemar_p(4, 0) == 0.125
    # b=10, c=0 -> 2 * 1/1024
    assert _exact_mcnemar_p(10, 0) == 0.001953
    # A balanced split is as unsupportive as it gets; the tail is clamped at 1.
    assert _exact_mcnemar_p(5, 5) == 1.0


def test_exact_mcnemar_is_symmetric_and_undefined_without_discordance():
    assert _exact_mcnemar_p(3, 7) == _exact_mcnemar_p(7, 3)
    assert _exact_mcnemar_p(0, 0) is None


def test_bootstrap_interval_is_reproducible_and_degenerate_for_a_flat_sample():
    values = [1.0, 0.0, 1.0, 1.0, 0.0, 1.0, 0.0, 1.0, 1.0, 1.0]
    first = _bootstrap_mean_ci(values)
    assert first == _bootstrap_mean_ci(values)
    assert first is not None and first[0] <= first[1]
    # An unseeded resample would give a different interval on every call, which
    # would break the "summary is recomputable from raw artifacts" guarantee.
    assert BOOTSTRAP_SEED == 20260101
    assert _bootstrap_mean_ci([0.5] * 10) == [0.5, 0.5]
    assert _bootstrap_mean_ci([]) is None


def test_mcnemar_is_reported_once_the_sample_clears_the_floor():
    # off succeeds 4/10, on succeeds 8/10 -> 4 discordant pairs, all favouring on.
    paired = build_summary(_pairs(10, off_until=4, on_until=8))["paired_effect"]

    assert paired["sample_count"] == 10
    assert paired["success_delta"] == 0.4
    assert paired["mcnemar"]["reported"] is True
    assert paired["mcnemar"]["on_only_success"] == 4
    assert paired["mcnemar"]["off_only_success"] == 0
    assert paired["mcnemar"]["concordant_pairs"] == 6
    # Four discordant pairs all pointing one way is still p=0.125: the exact test
    # is what stops a small tidy-looking delta from being read as an effect.
    assert paired["mcnemar"]["p_value"] == 0.125
    assert paired["mcnemar"]["significant"] is False
    assert paired["success_delta_bootstrap_ci_95"] is not None


def test_mcnemar_is_withheld_below_the_sample_floor():
    paired = build_summary(
        _pairs(MIN_PAIRED_SAMPLES - 1, off_until=1, on_until=8)
    )["paired_effect"]

    assert paired["sample_adequate"] is False
    assert paired["claim"] == "descriptive_only"
    assert paired["mcnemar"]["reported"] is False
    assert paired["mcnemar"]["p_value"] is None
    assert paired["mcnemar"]["significant"] is False
    assert paired["success_delta_bootstrap_ci_95"] is None


def test_a_unanimous_direction_becomes_significant_at_a_realistic_sample():
    # 10 discordant pairs all favouring `on` -> p = 2/1024.
    paired = build_summary(_pairs(10, off_until=0, on_until=10))["paired_effect"]

    assert paired["mcnemar"]["on_only_success"] == 10
    assert paired["mcnemar"]["p_value"] == 0.001953
    assert paired["mcnemar"]["significant"] is True


def test_arm_statistics_report_pass_at_one_and_turn_usage():
    records = [
        _record("on", 1, True, turns_used=3, solved_within_budget=True),
        _record("on", 2, True, turns_used=3, solved_within_budget=True),
        _record("on", 3, False, turns_used=12, solved_within_budget=False),
        {**_record("on", 4, False), "evaluation_report": None},
    ]

    stats = build_summary(records)["by_arm"]["on"]

    assert stats["attempts"] == 4
    # One attempt could not be scored, so it is excluded from the denominator
    # instead of being counted as a failure.
    assert stats["evaluated_attempts"] == 3
    assert stats["pass_at_1"] == round(2 / 3, 6)
    assert stats["turns_used_distribution"] == {3: 2, 12: 1}
    assert stats["average_turns_used"] == 6.0
    assert stats["solved_within_budget"] == {"true": 2, "false": 1, "unknown": 0}


def test_solved_within_budget_stays_unknown_when_no_case_declares_a_budget():
    stats = build_summary(_pairs(2, off_until=0, on_until=2))["by_arm"]["on"]

    assert stats["solved_within_budget"] == {"true": 0, "false": 0, "unknown": 2}
    assert stats["pass_at_1"] == 1.0


@pytest.mark.parametrize("arm", ["off", "on"])
def test_empty_summary_keeps_the_new_fields_unknown_rather_than_zero(arm):
    stats = build_summary([])["by_arm"][arm]

    assert stats["pass_at_1"] is None
    assert stats["average_turns_used"] is None
    assert stats["turns_used_distribution"] == {}
