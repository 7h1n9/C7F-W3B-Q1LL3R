"""A platform outage must not retire the backlog.

Measured on the live queue: **10 of 13 failures were** ``ARENA_MCP_UNREACHABLE``
(502 from a gateway in front of the platform).  Every one of them was charged to
``outcome_count`` -- the budget that retires an item after ``MAX_ATTEMPTS`` -- so
a platform outage quietly converted a batch of healthy challenges into FAILED
ones.  The queue looked like it was working the whole time.

What has to stay true:

* a failure that says nothing about the challenge never charges the retry budget;
* a real verdict (the solve ran and did not produce a usable result) still does;
* a rate limit keeps its existing, *different* treatment -- the claim is undone
  because the request was refused before it could use the environment;
* and the "never charge the budget" rule cannot become an unbounded spin, so a
  permanently dead endpoint eventually retires the item with an explicit platform
  reason rather than a challenge verdict.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.exceptions import DomainError
from app.services import arena_queue as queue

MCP_URL = "https://arena.example/mcp"


class _FakeSession:
    """The slice of ``AsyncSession`` the claim/mark/abandon paths touch."""

    def __init__(self, item: object, connection: object | None = None) -> None:
        self._item = item
        self._connection = connection
        self.commits = 0

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def get(self, model: object, key: object) -> object | None:
        name = getattr(model, "__name__", "")
        if name == "ArenaSolveQueueItem":
            return self._item
        if name == "ArenaConnection":
            return self._connection
        # SolveRun lookups belong to ``_abandon_run``, which no-ops when the
        # item never recorded a Run.
        return None

    async def commit(self) -> None:
        self.commits += 1


def _item(
    *,
    status: str = queue.RUNNING,
    attempt_count: int = 1,
    outcome_count: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        id="item-1",
        status=status,
        attempt_count=attempt_count,
        outcome_count=outcome_count,
        run_id=None,
        last_error=None,
        finished_at=None,
        started_at=None,
        verdict=None,
        connection_id="conn-1",
        practice_ground_id="pg-1",
        platform_challenge_id="ch-1",
        priority=100,
    )


@pytest.fixture(autouse=True)
def _no_real_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """The production back-off is 65s; the logic under test only needs it to run."""

    monkeypatch.setattr(queue, "RATE_LIMIT_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(queue, "PLATFORM_ENV_BUSY_BACKOFF_SECONDS", 0.0)


def _install(monkeypatch: pytest.MonkeyPatch, item: SimpleNamespace, connection: object | None = None):
    session = _FakeSession(item, connection)
    monkeypatch.setattr(queue, "SessionLocal", lambda: session)
    return session


# ── the predicates ───────────────────────────────────────────────────────────


def test_unreachable_error_is_not_a_verdict_on_the_challenge() -> None:
    error = DomainError(
        "ARENA_MCP_UNREACHABLE",
        f"Arena MCP endpoint is unreachable: connect error",
        {"url": MCP_URL},
        503,
        stage="ARENA_MCP",
        retryable=True,
    )

    assert queue._is_platform_unavailable(str(error), error.retryable) is True
    assert queue._is_rate_limit_error(str(error)) is False


def test_http_502_is_read_as_a_platform_fault_from_text_alone() -> None:
    """The message survives re-wrapping, so the markers are the fallback."""

    assert queue._is_platform_unavailable("ARENA_MCP_HTTP_ERROR: 502 Bad Gateway") is True
    assert queue._is_platform_unavailable("tool failed", None) is False


def test_a_rate_limit_is_not_classified_as_an_outage() -> None:
    """Branch order matters: a 429 is ``retryable`` too, but it is a *rate limit*."""

    assert queue._is_rate_limit_error("ARENA_MCP_HTTP_ERROR rate_limit_exceeded") is True
    assert queue._is_platform_unavailable("rate_limit_exceeded", True) is True


# ── the retry budget is not charged ──────────────────────────────────────────


async def test_platform_outage_does_not_burn_the_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _item(attempt_count=1, outcome_count=0)
    session = _install(monkeypatch, item)

    await queue.ArenaQueueScheduler()._mark_failed(
        "item-1", "ARENA_MCP_UNREACHABLE: connect error", retryable=True
    )

    assert item.outcome_count == 0, "an outage must not spend the retry budget"
    assert item.status == queue.PENDING, "the item must go back to the backlog"
    assert item.finished_at is None, "a requeued item must not look finished"
    assert session.commits == 1


@pytest.mark.parametrize(
    "message,retryable",
    [
        ("ARENA_MCP_UNREACHABLE: connect error", True),
        ("ARENA_MCP_HTTP_ERROR: 502 Bad Gateway", True),
        ("ARENA_MCP_HTTP_ERROR: 502 Bad Gateway", None),
        ("bad gateway", None),
        ("service unavailable", None),
    ],
)
async def test_every_outage_shape_spares_the_budget(
    monkeypatch: pytest.MonkeyPatch, message: str, retryable: bool | None
) -> None:
    item = _item(outcome_count=1)
    _install(monkeypatch, item)

    await queue.ArenaQueueScheduler()._mark_failed("item-1", message, retryable=retryable)

    assert item.outcome_count == 1
    assert item.status == queue.PENDING


async def test_a_genuine_failure_still_charges_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fix must not turn the budget off: a real verdict still retires."""

    item = _item(outcome_count=1)
    _install(monkeypatch, item)

    await queue.ArenaQueueScheduler()._mark_failed(
        "item-1", "Flag rejected by the platform.", retryable=False
    )

    assert item.outcome_count == 2
    assert item.status == queue.FAILED


async def test_the_budget_alone_does_not_retire_an_outage_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``outcome_count`` at the cap must not matter when the platform never answered."""

    item = _item(outcome_count=queue.MAX_ATTEMPTS, attempt_count=3)
    _install(monkeypatch, item)

    await queue.ArenaQueueScheduler()._mark_failed(
        "item-1", "ARENA_MCP_UNREACHABLE: connect error", retryable=True
    )

    assert item.status == queue.PENDING


# ── existing treatments are preserved ────────────────────────────────────────


async def test_rate_limit_still_undoes_the_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _item(attempt_count=4, outcome_count=0)
    _install(monkeypatch, item)

    await queue.ArenaQueueScheduler()._mark_failed(
        "item-1", "rate_limit_exceeded", retryable=True
    )

    assert item.attempt_count == 3, "a refused request must not keep its claim"
    assert item.outcome_count == 0
    assert item.status == queue.PENDING


async def test_held_environment_slot_still_undoes_the_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _item(attempt_count=2, outcome_count=0)
    _install(monkeypatch, item)

    await queue.ArenaQueueScheduler()._mark_failed(
        "item-1", "ARENA_LEASE_BUSY: another solve holds the environment"
    )

    assert item.attempt_count == 1
    assert item.status == queue.PENDING


async def test_platform_environment_limit_is_not_a_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The platform's own slot being occupied must not retire the item.

    Measured 2026-10-28 15:11 (local): a stale environment still occupied the
    platform's single slot, so every acquire was refused with
    ``agent_environment_limit``.  That code was not recognised as "slot taken",
    so it fell through to the spent-attempt branch and all 28 remaining items
    were written off ``FAILED`` inside two minutes -- none was ever evaluated.
    """

    item = _item(attempt_count=3, outcome_count=0)
    _install(monkeypatch, item)

    await queue.ArenaQueueScheduler()._mark_failed(
        "item-1",
        "('agent_environment_limit', 'This Agent already runs its maximum "
        "number of environments.')",
    )

    assert item.attempt_count == 2, "a refused acquire must not keep its claim"
    assert item.outcome_count == 0, "the challenge was never evaluated"
    assert item.status == queue.PENDING


async def test_outage_keeps_the_claim_it_consumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unlike a rate limit, the request reached the platform, so the claim stands.

    That is also what makes the attempt ceiling below reachable at all -- the
    rate-limit path decrements it back, so it could never bound anything.
    """

    item = _item(attempt_count=4, outcome_count=0)
    _install(monkeypatch, item)

    await queue.ArenaQueueScheduler()._mark_failed(
        "item-1", "ARENA_MCP_UNREACHABLE: connect error", retryable=True
    )

    assert item.attempt_count == 4
    assert item.status == queue.PENDING


# ── unbounded spinning is bounded ────────────────────────────────────────────


async def test_a_permanently_dead_endpoint_retires_with_a_platform_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _item(attempt_count=queue.PLATFORM_RETRY_MAX_CLAIMS, outcome_count=0)
    _install(monkeypatch, item)

    await queue.ArenaQueueScheduler()._mark_failed(
        "item-1", "ARENA_MCP_UNREACHABLE: connect error", retryable=True
    )

    assert item.status == queue.FAILED
    assert "never evaluated" in str(item.last_error), (
        "the reason must make clear this was not a challenge verdict"
    )
    assert item.outcome_count == 0, "even the retirement must not fake a verdict"


async def test_the_ceiling_is_checked_against_the_claim_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    just_under = _item(attempt_count=queue.PLATFORM_RETRY_MAX_CLAIMS - 1)
    _install(monkeypatch, just_under)

    await queue.ArenaQueueScheduler()._mark_failed(
        "item-1", "ARENA_MCP_UNREACHABLE: connect error", retryable=True
    )

    assert just_under.status == queue.PENDING


# ── the claim path actually forwards the signal ──────────────────────────────


async def test_process_forwards_the_structured_retryable_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The end-to-end wiring: a ``DomainError`` from the solve must reach the branch."""

    item = _item(status=queue.PENDING, attempt_count=0, outcome_count=0)
    connection = SimpleNamespace(enabled=True)
    _install(monkeypatch, item, connection)

    async def _explode(*_args: object, **_kwargs: object) -> object:
        raise DomainError(
            "ARENA_MCP_UNREACHABLE",
            "Arena MCP endpoint is unreachable: connect error",
            {"url": MCP_URL},
            503,
            stage="ARENA_MCP",
            retryable=True,
        )

    monkeypatch.setattr(queue.arena_solve, "solve_one", _explode)

    await queue.ArenaQueueScheduler()._process("item-1")

    assert item.outcome_count == 0, "the outage was charged to the retry budget"
    assert item.status == queue.PENDING
    assert item.attempt_count == 1, "the claim made by _process should stand"
