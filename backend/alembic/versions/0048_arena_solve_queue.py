"""Add the arena batch-solve queue.

A batch spans hours because the arena serves one environment at a time, so the
backlog has to survive a restart -- and, more importantly, must not re-solve a
challenge that already passed.  The unique constraint on
``(connection_id, practice_ground_id, platform_challenge_id)`` is what makes
re-enqueueing idempotent; ``attempt_count`` sits outside it so a failed item is
retried by updating the row rather than inserting a second one.

Inspector-guarded so the migration replays safely on a partially-applied database.
"""

import sqlalchemy as sa

from alembic import op

revision = "0048_arena_solve_queue"
down_revision = "0047_arena_default_model"
branch_labels = None
depends_on = None

TABLE = "arena_solve_queue"

COLUMNS = [
    sa.Column("id", sa.String(length=36), nullable=False),
    sa.Column("created_at", sa.DateTime(), nullable=False),
    sa.Column("connection_id", sa.String(length=36), nullable=False),
    sa.Column("practice_ground_id", sa.String(length=64), nullable=False),
    sa.Column("platform_challenge_id", sa.String(length=64), nullable=False),
    sa.Column("challenge_name", sa.String(length=200), nullable=False, server_default=""),
    sa.Column("category", sa.String(length=60), nullable=False, server_default=""),
    sa.Column("difficulty", sa.String(length=40), nullable=False, server_default=""),
    sa.Column("has_container", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    sa.Column("status", sa.String(length=20), nullable=False, server_default="PENDING"),
    sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
    sa.Column("verdict", sa.String(length=30), nullable=True),
    sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("last_error", sa.Text(), nullable=True),
    sa.Column("run_id", sa.String(length=36), nullable=True),
    sa.Column("started_at", sa.DateTime(), nullable=True),
    sa.Column("finished_at", sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint("id"),
    sa.ForeignKeyConstraint(["connection_id"], ["arena_connections.id"]),
]


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if TABLE in _tables():
        return
    op.create_table(TABLE, *COLUMNS)
    # The pick query is "oldest highest-priority pending item", so the index
    # order matches the ORDER BY rather than the column order of the table.
    op.create_index("ix_arena_queue_pick", TABLE, ["status", "priority", "created_at"])
    op.create_index("ix_arena_solve_queue_connection_id", TABLE, ["connection_id"])
    op.create_index("ix_arena_solve_queue_status", TABLE, ["status"])
    op.create_unique_constraint(
        "uq_arena_queue_challenge",
        TABLE,
        ["connection_id", "practice_ground_id", "platform_challenge_id"],
    )


def downgrade() -> None:
    if TABLE not in _tables():
        return
    op.drop_table(TABLE)
