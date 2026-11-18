"""A flag an engine reports must reach the platform's flag ledger.

The regression: Muteki published its flag only as a ``muteki.flag_found`` run
event.  ``flag_candidates`` -- the table the benchmark evaluator, the UI and the
submit path all read -- stayed empty, so Run ``9e095f0c`` reached
``COMPLETED_SOLVED`` holding the byte-exact flag while the evaluator recorded
``verified_flag_candidates: 0`` and scored the arm ``INCOMPLETE``.

Two rules are pinned here:

* the host re-verifies the value against the Challenge's declared banner rather
  than trusting the runtime's ``verified`` bit;
* a value that fails that check is recorded as ``OPEN`` for an operator, never
  as a solve.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services import flags as flags_module
from app.services.flags import flag_service


class _FakeSession:
    """Enough of ``AsyncSession`` for a single-row upsert."""

    def __init__(self, existing=None) -> None:
        self._existing = existing
        self.added: list[object] = []
        self.commits = 0

    async def scalar(self, _statement):
        return self._existing

    def add(self, item) -> None:
        self.added.append(item)

    async def flush(self) -> None:
        # The real session assigns the UUID on flush; mirror just enough of that
        # so the provenance row does not carry ``None``.
        for item in self.added:
            if getattr(item, "id", None) is None and type(item).__name__ == "FlagCandidate":
                item.id = "candidate-1"

    async def commit(self) -> None:
        self.commits += 1


def _run(**overrides) -> SimpleNamespace:
    values = {"id": "run-1", "assistance_level": "AUTONOMOUS"}
    values.update(overrides)
    return SimpleNamespace(**values)


def _challenge(pattern: str = r"flag\{[^}]+\}") -> SimpleNamespace:
    return SimpleNamespace(id="challenge-1", flag_pattern=pattern)


@pytest.fixture(autouse=True)
def _silence_events(monkeypatch):
    async def _append(_session, _run_id, _event_type, _payload):
        return SimpleNamespace(sequence=1)

    monkeypatch.setattr(flags_module.event_service, "append", _append)
    yield


def test_a_matching_flag_is_recorded_as_valid() -> None:
    session = _FakeSession()
    item = asyncio.run(
        flag_service.record_runtime_flag(
            session, _run(), _challenge(), "flag{93ffaeedc961d108d17fddd66b90487d}"
        )
    )
    assert item is not None
    assert item.verified is True
    assert item.review_state == "VALID"
    assert item.first_seen_source_type == "MUTEKI_RUNTIME"
    assert session.commits == 1


def test_a_banner_mismatch_lands_open_instead_of_solved() -> None:
    session = _FakeSession()
    item = asyncio.run(
        flag_service.record_runtime_flag(
            session, _run(), _challenge(r"CTF2\{[^}]+\}"), "flag{looks_right_but_is_not}"
        )
    )
    assert item is not None
    assert item.verified is False
    assert item.review_state == "OPEN"


def test_an_empty_value_records_nothing() -> None:
    session = _FakeSession()
    assert asyncio.run(flag_service.record_runtime_flag(session, _run(), _challenge(), "   ")) is None
    assert session.added == []


def test_an_existing_unverified_row_is_upgraded_once_it_matches() -> None:
    existing = SimpleNamespace(
        id="candidate-1",
        verified=False,
        review_state="OPEN",
        pattern_matched=False,
    )
    session = _FakeSession(existing=existing)
    item = asyncio.run(
        flag_service.record_runtime_flag(session, _run(), _challenge(), "flag{abc}")
    )
    assert item is existing
    assert existing.verified is True
    assert existing.review_state == "VALID"
