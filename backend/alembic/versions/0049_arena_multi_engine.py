"""Add multi-engine and coordinator-model selection to arena connections.

Muteki can run several Worker engines at once and one Coordinator that reasons
over their results.  The existing ``default_model_config_id`` covers the
single-engine case; this adds the other two so an arena solve can use the same
setup the UI already offers for a manual Run.

``default_worker_engines`` is a JSON column, not a child table: the list is
always read and written whole and is bounded by the runtime's engine types, so a
join table would add a migration per new engine without enabling any query the
application makes.

The JSON column is added in three steps rather than one ``NOT NULL DEFAULT``.
MySQL refuses a literal default on JSON columns ("BLOB, TEXT, GEOMETRY or JSON
column can't have a default value"), so the column is created nullable, filled,
then tightened.  A generated default via ``DEFAULT (JSON_ARRAY())`` would also
work on MySQL 8.0.13+, but it would tie this migration to a server version for
no benefit the three-step form does not already provide.

Inspector-guarded so the migration replays safely on a partially-applied
database -- which is not hypothetical: an earlier attempt at this migration
added the first column and failed on the second.
"""

import sqlalchemy as sa

from alembic import op

revision = "0049_arena_multi_engine"
down_revision = "0048_arena_solve_queue"
branch_labels = None
depends_on = None

TABLE = "arena_connections"
REASON_COLUMN = "default_reason_model_config_id"
ENGINES_COLUMN = "default_worker_engines"


def _existing_columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if TABLE not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(TABLE)}


def upgrade() -> None:
    existing = _existing_columns()

    if REASON_COLUMN not in existing:
        op.add_column(TABLE, sa.Column(REASON_COLUMN, sa.String(length=36), nullable=True))

    if ENGINES_COLUMN not in existing:
        op.add_column(TABLE, sa.Column(ENGINES_COLUMN, sa.JSON(), nullable=True))
        # Existing rows predate multi-engine selection, and an empty list is
        # exactly what they mean: fall back to the single default model.
        op.execute(f"UPDATE {TABLE} SET {ENGINES_COLUMN} = '[]' WHERE {ENGINES_COLUMN} IS NULL")
        op.alter_column(TABLE, ENGINES_COLUMN, existing_type=sa.JSON(), nullable=False)


def downgrade() -> None:
    existing = _existing_columns()
    for column in (ENGINES_COLUMN, REASON_COLUMN):
        if column in existing:
            op.drop_column(TABLE, column)
