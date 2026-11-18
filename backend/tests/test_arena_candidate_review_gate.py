"""A wrong-banner flag waits for a human instead of being spent on a submission.

Run ``49af30bf`` submitted ``flag{moxiaoxi}`` -- the flag of the ORIGINAL public
challenge, not the declared ``CTF2{...}`` shape -- and the platform rejected it.
The value should never have reached the platform automatically: it does not fit
the banner the operator declared, which is the signature of something recovered
from somewhere other than the target.

So a candidate that does not match the declared banner and has not been judged
is **held**.  It is shown to the operator and promoted to ``VALID`` only by them,
and only that promotion lets it be submitted.  A candidate that DOES match the
banner keeps the old behaviour -- auto-submission exists so a batch can run
unattended, and narrowing it to human-approved values only would stop the batch
dead.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import arena_solve


class _Rows:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return self._rows


class _FakeSession:
    """Mimics the one driver behaviour that matters: ``scalars`` keeps column 0.

    An earlier version of this fake handed whole tuples back from ``scalars``,
    which the real driver never does.  So a three-column select read through
    ``scalars`` passed review here and then crashed in production, on the runs
    that mattered: a solved Run reaches the held-candidate count with a real flag
    string in hand, and unpacking that string into three names raises "too many
    values to unpack (expected 3)" -- after the flag was found and before it was
    submitted.  A fake that is wrong in a different direction than production
    tests nothing, so this one is wrong in the same direction on purpose.
    """

    def __init__(self, rows: list, declared_pattern: str | None = r"CTF2\{[^}]+\}") -> None:
        self._rows = rows
        #: What ``_load_candidates`` reads when it asks the challenge for its
        #: declared ``flag_pattern``.  Defaults to the arena's own banner because
        #: that is the live configuration; a test can pass ``None`` for the
        #: "pattern not registered" case.
        self._declared_pattern = declared_pattern

    async def scalars(self, _statement: object) -> _Rows:
        return _Rows([row[0] if isinstance(row, tuple) else row for row in self._rows])

    async def scalar(self, _statement: object) -> object:
        return self._declared_pattern

    async def execute(self, _statement: object) -> _Rows:
        return _Rows(self._rows)


def _candidate(
    value: str,
    *,
    review_state: str = "OPEN",
    pattern_matched: bool = False,
    created_at: object | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        candidate=value,
        review_state=review_state,
        pattern_matched=pattern_matched,
        created_at=created_at or SimpleNamespace(),
    )


@pytest.mark.asyncio
async def test_a_wrong_banner_candidate_is_submitted() -> None:
    """The box's own wrapper is not a reason to withhold the value.

    The arena mints a per-instance flag, so a value can be the answer while its
    wrapper came from the box -- ``n1book{afr_1_solved}`` on the N1BOOK targets
    is exactly that.  Run ``de9ab053`` recorded it and then submitted only the
    host-invented ``CTF2{afr_1_solved}``, which the platform rejected, so the one
    value that would have been accepted was never sent.

    The verdict is recorded per attempt, so a wrong guess costs one submission
    row and cannot be mistaken for a solve.
    """

    session = _FakeSession([_candidate("flag{moxiaoxi}", pattern_matched=False)])
    found = await arena_solve._load_candidates(session, "run-1")
    # As printed first, then the same value under the declared and common banners.
    assert found[0] == "flag{moxiaoxi}"
    assert "CTF2{moxiaoxi}" in found


@pytest.mark.asyncio
async def test_a_matching_candidate_is_still_submitted() -> None:
    """The batch must keep running unattended for values that do fit."""

    session = _FakeSession([_candidate("CTF2{abc}", pattern_matched=True)])
    found = await arena_solve._load_candidates(session, "run-1")
    assert found[0] == "CTF2{abc}"


@pytest.mark.asyncio
async def test_a_human_validated_candidate_is_submitted_whatever_its_shape() -> None:
    """The operator's decision is the whole point of the hold."""

    session = _FakeSession(
        [_candidate("flag{moxiaoxi}", review_state="VALID", pattern_matched=False)]
    )
    found = await arena_solve._load_candidates(session, "run-1")
    assert found[0] == "flag{moxiaoxi}"


@pytest.mark.asyncio
async def test_a_placeholder_is_not_rescued_by_a_validation() -> None:
    """Sanity: an empty value never reaches the submitter regardless of state."""

    session = _FakeSession([_candidate("", review_state="VALID", pattern_matched=True)])
    assert await arena_solve._load_candidates(session, "run-1") == []


@pytest.mark.asyncio
async def test_the_matching_candidate_is_tried_first() -> None:
    """Ordering, not exclusion: the likely answer goes first, the rest follow.

    A batch has to exhaust the plausible values within one environment lease --
    the slot is global and short-lived -- so everything is tried, in rank order.
    """

    session = _FakeSession(
        [
            _candidate("flag{moxiaoxi}", pattern_matched=False),
            _candidate("CTF2{abc}", pattern_matched=True),
        ]
    )
    found = await arena_solve._load_candidates(session, "run-1")
    # The pattern-matching value is tried first, and its as-printed spelling leads.
    assert found[0] == "CTF2{abc}"
    assert "flag{moxiaoxi}" in found


@pytest.mark.asyncio
async def test_a_rejected_candidate_is_never_submitted_again() -> None:
    """``INVALID`` is the operator's final answer, not a smaller ``OPEN``.

    Reproduction: run ``49af30bf``'s ``flag{moxiaoxi}`` was judged ``INVALID``
    after the platform rejected it, and the gate still returned it as
    submittable -- the one verdict the loop was ignoring was the human's.
    """

    session = _FakeSession([_candidate("flag{moxiaoxi}", review_state="INVALID", pattern_matched=False)])
    assert await arena_solve._load_candidates(session, "run-1") == []


@pytest.mark.asyncio
async def test_a_rejected_candidate_is_not_rescued_by_the_banner() -> None:
    """A rejection outranks a pattern match; the human looked at it."""

    session = _FakeSession([_candidate("CTF2{abc}", review_state="INVALID", pattern_matched=True)])
    assert await arena_solve._load_candidates(session, "run-1") == []


@pytest.mark.asyncio
async def test_a_rejected_candidate_does_not_hide_a_live_one() -> None:
    session = _FakeSession(
        [
            _candidate("flag{moxiaoxi}", review_state="INVALID", pattern_matched=False),
            _candidate("CTF2{abc}", review_state="OPEN", pattern_matched=True),
        ]
    )
    found = await arena_solve._load_candidates(session, "run-1")
    assert found[0] == "CTF2{abc}"
    # The rejected value's *inner text* must not sneak back in under another
    # banner: an INVALID verdict is final for the value, not just for its wrapper.
    assert not any("moxiaoxi" in spelling for spelling in found)


@pytest.mark.asyncio
async def test_held_candidates_are_counted_for_the_operator() -> None:
    """A Run that submits nothing must still say that it held something."""

    session = _FakeSession(
        [
            ("flag{moxiaoxi}", "OPEN", 0),
            ("CTF2{abc}", "OPEN", 1),
            ("flag{other}", "VALID", 0),
            ("", "OPEN", 0),
        ]
    )
    assert await arena_solve._count_held_candidates(session, "run-1") == 1


@pytest.mark.asyncio
async def test_a_real_length_flag_is_counted_rather_than_crashing() -> None:
    """The regression this file exists for, pinned by the shape that broke it.

    ``CTF2{a569d9ab-41f5-4956-a3c7-b48a0917f155}`` -- the first flag the arena
    loop ever extracted.  The Run reported ``COMPLETED_SOLVED`` and the candidate
    table held the value, then the counting helper read the three columns through
    ``scalars`` and unpacked the flag string instead of a row.
    """

    flag = "CTF2{a569d9ab-41f5-4956-a3c7-b48a0917f155}"
    session = _FakeSession([(flag, "OPEN", 0)])
    assert await arena_solve._count_held_candidates(session, "run-1") == 1


@pytest.mark.asyncio
async def test_the_box_spelling_comes_before_any_banner_we_add() -> None:
    """Order is the contract the operator asked for, so pin it exactly.

    Measured twice on the night of 2026-10-23: ``flag{r0bots_...}`` and
    ``n1book{afr_1_solved}`` were accepted *after* the ``CTF2{...}`` spelling of
    the same value had been rejected one second earlier.  The wrapper the box
    printed is evidence; the wrapper we invent is a guess.  So a value is offered
    as printed first, then under the declared banner, then the common family --
    never the reverse, or a Run with a submission budget would spend it all on
    guessed wrappers and never send the one spelling that works.
    """

    session = _FakeSession([_candidate("n1book{afr_1_solved}", pattern_matched=False)])
    found = await arena_solve._load_candidates(session, "run-1")
    assert found[0] == "n1book{afr_1_solved}"
    assert found[1] == "CTF2{afr_1_solved}", "declared banner is tried right after the box's own"
    assert "flag{afr_1_solved}" in found


@pytest.mark.asyncio
async def test_a_bare_value_is_wrapped_and_the_declared_banner_leads() -> None:
    """A value with no wrapper still gets spellings, declared banner first."""

    session = _FakeSession([_candidate("afr_1_solved", pattern_matched=False)])
    found = await arena_solve._load_candidates(session, "run-1")
    assert found[0] == "afr_1_solved", "the raw value is still tried as-is"
    assert found[1] == "CTF2{afr_1_solved}"


@pytest.mark.asyncio
async def test_a_malformed_wrapper_is_not_rewrapped() -> None:
    """A half-broken ``{...}`` cannot be a value, so it is dropped, not guessed at."""

    session = _FakeSession([_candidate("broken{value", pattern_matched=False)])
    found = await arena_solve._load_candidates(session, "run-1")
    assert found == ["broken{value"], "no invented spellings from an unparseable string"
