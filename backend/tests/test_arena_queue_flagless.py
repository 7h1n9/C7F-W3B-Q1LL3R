"""The queue must not spend runs on challenges that cannot produce a flag.

``[5.1.3 摩斯密码][无flag]`` and ``[5.1.4 仿射密码][无flag]`` are offline
puzzles whose titles say they carry no flag -- yet they sat ACTIVE with the
default ``flag\\{[^}]+\\}`` pattern, and both had already reached
``COMPLETED_UNSOLVED``.  ``arena_queue`` already documents the general problem
("an attachment-only challenge in this catalogue is usually a teaching exercise
with no submittable flag"), but nothing enforced it.

The marker lives in ``challenges.metadata_json`` because
``upsert_local_challenge`` merges with ``{**existing.metadata_json, ...}``, so a
key we add survives re-import and needs no migration.
"""

from __future__ import annotations

import json

import pytest

from app.models.arena import ArenaSolveQueueItem
from app.models.challenge import Challenge
from app.services.arena_queue import _flagless_platform_ids


class FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class FakeSession:
    """Minimal stand-in that returns whatever rows the test seeds.

    The real query filters in SQL with a LIKE over the JSON column; that part is
    only there to keep the scan small, so the tests target the Python-side
    matching instead of re-implementing a database.
    """

    def __init__(self, rows):
        self._rows = rows
        self.queries = 0

    async def scalars(self, _stmt):
        self.queries += 1
        return FakeScalars(self._rows)


def challenge(arena_id: str, *, no_flag: bool, name: str = "c") -> Challenge:
    meta = {"arena_challenge_id": arena_id, "has_container": False}
    if no_flag:
        meta["no_flag_challenge"] = True
    row = Challenge(name=name, challenge_type="WEB_TARGET")
    row.metadata_json = meta
    return row


@pytest.mark.anyio
async def test_marked_challenge_is_reported_as_flagless():
    session = FakeSession([challenge("arena-1", no_flag=True)])
    flagged = await _flagless_platform_ids(session, {"arena-1"})
    assert flagged == {"arena-1"}


@pytest.mark.anyio
async def test_unmarked_challenge_is_left_alone():
    session = FakeSession([challenge("arena-2", no_flag=False)])
    flagged = await _flagless_platform_ids(session, {"arena-2"})
    assert flagged == set()


@pytest.mark.anyio
async def test_metadata_stored_as_a_json_string_is_still_understood():
    """Older rows come back as a string, newer ones as a dict.

    A ``JSON_EXTRACT`` predicate would silently miss whichever half does not
    match, which is why the check runs in Python.
    """

    row = Challenge(name="legacy", challenge_type="WEB_TARGET")
    row.metadata_json = json.dumps(
        {"arena_challenge_id": "arena-3", "no_flag_challenge": True}
    )
    session = FakeSession([row])
    assert await _flagless_platform_ids(session, {"arena-3"}) == {"arena-3"}


@pytest.mark.anyio
async def test_challenge_outside_the_candidate_set_is_ignored():
    session = FakeSession([challenge("arena-4", no_flag=True)])
    assert await _flagless_platform_ids(session, {"arena-other"}) == set()


@pytest.mark.anyio
async def test_broken_metadata_does_not_crash_the_queue():
    row = Challenge(name="broken", challenge_type="WEB_TARGET")
    row.metadata_json = "{not valid json"
    session = FakeSession([row, challenge("arena-5", no_flag=True)])
    flagged = await _flagless_platform_ids(session, {"arena-5"})
    assert flagged == {"arena-5"}, "one bad row must not hide the good ones"


@pytest.mark.anyio
async def test_missing_challenge_row_is_not_treated_as_flagless():
    """A queue item outlives its challenge row; absence is not a marker."""

    session = FakeSession([])
    assert await _flagless_platform_ids(session, {"arena-6"}) == set()


@pytest.mark.anyio
async def test_empty_candidate_set_skips_the_query_entirely():
    session = FakeSession([challenge("arena-7", no_flag=True)])
    assert await _flagless_platform_ids(session, set()) == set()
