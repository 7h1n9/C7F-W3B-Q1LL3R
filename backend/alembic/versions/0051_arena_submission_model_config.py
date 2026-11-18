"""Detach arena rows from a deleted model configuration.

``0044_model_config_deletable`` made every reference to ``model_configs`` that
existed at the time deletable, but the arena tables were created afterwards
(``0046_arena_integration`` / ``0047_arena_default_model``) and carry the MySQL
default ``RESTRICT``.  A configuration that had ever attested an arena flag, or
that an arena connection used as its default, was therefore still undeletable
and the API surfaced the ``IntegrityError 1451`` as a blanket HTTP 500 -- the
exact failure 0044 was written to remove, reintroduced by tables that did not
exist when it ran.

Both columns are optional: a submission keeps its verdict and attested flag, a
connection keeps its endpoint and token.  ``SET NULL`` keeps the record and
drops only the attribution, which is the rule 0044 already states -- deleting a
model configuration must never delete run or arena history.

Guard-shaped like 0044, so it is safe to replay and a no-op wherever the key is
absent.
"""

import sqlalchemy as sa

from alembic import op

revision = "0051_arena_submission_model_config"
down_revision = "0050_arena_queue_outcome_count"
branch_labels = None
depends_on = None

# child table -> the column that points at model_configs
TARGETS = {
    "arena_submissions": "model_config_id",
    # An arena connection falls back to its own engine roster when the default
    # model disappears; the endpoint and credentials are unaffected.
    "arena_connections": "default_model_config_id",
}


def _existing_fk(table: str, column: str) -> dict | None:
    """Return the foreign key on ``table.column``, whatever its name.

    The live schema carries MySQL auto-generated ``*_ibfk_N`` names while other
    environments may use the named form, so look the key up by column rather
    than by name.
    """

    for constraint in sa.inspect(op.get_bind()).get_foreign_keys(table):
        if constraint.get("constrained_columns") == [column]:
            return constraint
    return None


def _retarget(table: str, column: str, ondelete: str) -> None:
    if table not in sa.inspect(op.get_bind()).get_table_names():
        return
    constraint = _existing_fk(table, column)
    if constraint is None:
        return
    if (constraint.get("options") or {}).get("ondelete", "").upper() == ondelete:
        return
    op.drop_constraint(constraint["name"], table, type_="foreignkey")
    op.create_foreign_key(
        f"fk_{table}_{column}",
        table,
        "model_configs",
        [column],
        ["id"],
        ondelete=ondelete,
    )


def upgrade() -> None:
    if "model_configs" not in sa.inspect(op.get_bind()).get_table_names():
        return
    for table, column in TARGETS.items():
        _retarget(table, column, "SET NULL")


def downgrade() -> None:
    if "model_configs" not in sa.inspect(op.get_bind()).get_table_names():
        return
    for table, column in TARGETS.items():
        _retarget(table, column, "RESTRICT")
