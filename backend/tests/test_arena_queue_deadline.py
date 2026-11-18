"""A batch needs a wall-clock cutoff, not just an empty backlog.

Measured 2026-11-02: the batch was started at 00:14 for a 06:30 stop and was
still draining at 07:50 -- an hour and twenty minutes of quota spent on
challenges nobody had budgeted for, because ``_loop`` only stops when the
backlog empties or an operator calls ``/queue/stop`` by hand.

These tests pin the two things that make the cutoff safe: it is parsed in the
operator's local time (never UTC, which would fire eight hours off here), and it
is checked *between* items so a solve in flight is never cut in half.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.arena_queue import ArenaQueueScheduler, parse_stop_at


def test_an_iso_deadline_keeps_its_offset() -> None:
    parsed = parse_stop_at("2026-11-03T06:30:00+08:00")

    assert parsed is not None
    assert parsed.utcoffset() == timedelta(hours=8)
    assert (parsed.hour, parsed.minute) == (6, 30)


def test_a_bare_time_is_local_wall_clock() -> None:
    """``06:30`` is what an operator types; reading it as UTC fires 8h early."""

    parsed = parse_stop_at("06:30")

    assert parsed is not None
    assert (parsed.hour, parsed.minute) == (6, 30)
    assert parsed.utcoffset() == datetime.now().astimezone().utcoffset()


def test_a_bare_time_already_past_means_tomorrow() -> None:
    """Asking for 06:30 at 07:00 schedules the next one, not an instant stop."""

    now = datetime.now().astimezone()
    already = (now - timedelta(minutes=5)).strftime("%H:%M")
    parsed = parse_stop_at(already)

    assert parsed is not None
    assert parsed > datetime.now(UTC)


def test_no_deadline_is_the_old_behaviour() -> None:
    assert parse_stop_at(None) is None
    assert parse_stop_at("") is None
    assert parse_stop_at("   ") is None


@pytest.mark.parametrize("value", ["lol", "25:99", "06:30:00+99:00", "6"])
def test_a_malformed_deadline_is_refused(value: str) -> None:
    """Silently ignoring it would leave the operator believing the cutoff is set."""

    with pytest.raises(ValueError):
        parse_stop_at(value)


def test_a_passed_deadline_reads_as_reached() -> None:
    scheduler = ArenaQueueScheduler()

    assert scheduler._deadline_reached() is False
    scheduler._stop_at = datetime.now(UTC) - timedelta(seconds=1)
    assert scheduler._deadline_reached() is True


def test_a_naive_deadline_is_read_in_local_time() -> None:
    """A datetime stored without tzinfo must not be treated as UTC."""

    scheduler = ArenaQueueScheduler()
    # One hour ahead in *local* terms.  Interpreted as UTC on a +08:00 host this
    # would sit seven hours in the future and never fire during the batch.
    scheduler._stop_at = datetime.now() + timedelta(hours=1)

    assert scheduler._deadline_reached() is False


def test_the_health_snapshot_exposes_the_deadline() -> None:
    scheduler = ArenaQueueScheduler()
    health = scheduler.health()

    assert health["stop_at"] is None
    assert health["stop_reason"] is None

    scheduler._stop_at = datetime(2026, 11, 3, 6, 30, tzinfo=UTC)
    assert scheduler.health()["stop_at"] == "2026-11-03T06:30:00+00:00"
