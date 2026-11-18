"""Model-configuration deletion contract.

A configuration used by any Run used to be permanently undeletable: every
foreign key on ``model_configs`` was ``RESTRICT``, so the delete failed inside
the driver and escaped as a blanket HTTP 500.  These tests pin the two halves of
the fix -- the API must report a cause instead of a 500, and the schema must
actually allow the delete (see the 0044 migration).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from app.api.v1 import models
from app.core.exceptions import DomainError


class _FakeSession:
    """Minimal AsyncSession stand-in: get / delete / commit / rollback / scalar."""

    def __init__(self, *, item: object | None, counts: dict[str, int] | None = None, fail_on_commit: bool = False) -> None:
        self.item = item
        self.counts = counts or {}
        self.fail_on_commit = fail_on_commit
        self.deleted = False
        self.commits = 0
        self.rollbacks = 0

    async def get(self, _model: object, _identity: str) -> object | None:
        return self.item

    async def delete(self, _item: object) -> None:
        self.deleted = True

    async def commit(self) -> None:
        if self.fail_on_commit:
            # Mirrors what asyncmy raises for ERROR 1451.
            raise IntegrityError("DELETE FROM model_configs", {}, Exception("(1451, 'Cannot delete or update a parent row')"))
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def scalar(self, statement: object) -> int:
        # Each count statement is built per reference table; recover which table
        # it counts from its FROM clause so the counts are keyed like production.
        table = statement.get_final_froms()[0].name  # type: ignore[attr-defined]
        return self.counts.get(str(table), 0)


def _config(config_id: str = "cfg-1") -> SimpleNamespace:
    return SimpleNamespace(id=config_id, name="GLM", provider_type="openai_compatible")


# ── delete ──────────────────────────────────────────────────────────────────


def test_a_config_that_no_run_uses_is_deleted() -> None:
    session = _FakeSession(item=_config(), counts={})
    asyncio.run(models.delete_model_config("cfg-1", session))  # type: ignore[arg-type]
    assert session.deleted is True
    assert session.commits == 1
    assert session.rollbacks == 0


def test_a_missing_config_is_a_404_not_a_delete() -> None:
    session = _FakeSession(item=None)
    with pytest.raises(DomainError) as captured:
        asyncio.run(models.delete_model_config("absent", session))  # type: ignore[arg-type]
    assert captured.value.code == "MODEL_CONFIG_NOT_FOUND"
    assert captured.value.status_code == 404


def test_a_foreign_key_conflict_becomes_an_explainable_409() -> None:
    # The regression: this used to escape the handler as an unhandled
    # IntegrityError, i.e. a 500 "Backend request failed." with no cause.
    session = _FakeSession(
        item=_config(),
        counts={"solve_runs": 18, "run_attempts": 20},
        fail_on_commit=True,
    )
    with pytest.raises(DomainError) as captured:
        asyncio.run(models.delete_model_config("cfg-1", session))  # type: ignore[arg-type]

    error = captured.value
    assert error.code == "MODEL_CONFIG_IN_USE"
    assert error.status_code == 409
    # The message must name the blocking tables so the failure is actionable.
    assert "solve_runs" in error.message
    assert "run_attempts" in error.message
    assert error.details["blocking"] == ["run_attempts", "solve_runs"]
    assert error.details["references"]["solve_runs"] == 18
    # A failed delete must not leave the session dirty.
    assert session.rollbacks == 1
    assert session.commits == 0


def test_the_conflict_message_only_lists_non_zero_tables() -> None:
    session = _FakeSession(item=_config(), counts={"solve_runs": 0, "agent_turns": 3}, fail_on_commit=True)
    with pytest.raises(DomainError) as captured:
        asyncio.run(models.delete_model_config("cfg-1", session))  # type: ignore[arg-type]
    assert captured.value.details["blocking"] == ["agent_turns"]
    assert "solve_runs" not in captured.value.message


# ── usage report ────────────────────────────────────────────────────────────


def test_usage_reports_every_referencing_table() -> None:
    session = _FakeSession(item=_config(), counts={"solve_runs": 18, "run_attempts": 20})
    payload = asyncio.run(models.model_config_usage("cfg-1", session))  # type: ignore[arg-type]
    data = payload["data"]
    assert data["id"] == "cfg-1"
    assert data["name"] == "GLM"
    # Every reference is detachable after the 0044 migration.
    assert data["delete_safe"] is True
    assert data["blocking"] == []
    # All five referencing tables are reported, including the zero counts --
    # an absent key would be indistinguishable from "table not checked".
    assert set(data["references"]) == {
        "solve_runs",
        "run_attempts",
        "agent_turns",
        "challenge_conversations",
        "model_skill_bindings",
    }
    assert data["references"]["solve_runs"] == 18


def test_usage_on_a_missing_config_is_a_404() -> None:
    session = _FakeSession(item=None)
    with pytest.raises(DomainError) as captured:
        asyncio.run(models.model_config_usage("absent", session))  # type: ignore[arg-type]
    assert captured.value.code == "MODEL_CONFIG_NOT_FOUND"


# ── schema ──────────────────────────────────────────────────────────────────


def test_the_schema_allows_deleting_a_referenced_config() -> None:
    """The API can only succeed if the database lets the parent row go.

    Reads the live foreign keys rather than the ORM metadata, because the ORM
    does not carry ``ondelete`` for these columns -- the migration owns them.
    """

    import sqlalchemy as sa

    from app.core.config import get_settings

    url = get_settings().database_url
    if "mysql" not in url:
        pytest.skip("foreign-key delete rules are MySQL-specific")

    engine = sa.create_engine(url.replace("+asyncmy", "+pymysql"))
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                sa.text(
                    "SELECT TABLE_NAME, DELETE_RULE FROM information_schema.REFERENTIAL_CONSTRAINTS "
                    "WHERE CONSTRAINT_SCHEMA = DATABASE() AND REFERENCED_TABLE_NAME = 'model_configs'"
                )
            ).all()
    finally:
        engine.dispose()

    rules = {row[0]: row[1] for row in rows}
    assert rules, "model_configs has no inbound foreign keys; the schema changed"
    for table, rule in rules.items():
        assert rule != "NO ACTION", f"{table} still RESTRICTs a config delete"
        assert rule in {"SET NULL", "CASCADE"}, f"{table} has an unexpected rule: {rule}"
