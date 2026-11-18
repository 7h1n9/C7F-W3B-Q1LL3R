"""Add the default solve runtime to an arena connection.

Starting a solve from the arena needs to know which model and engine to run.
That choice belongs on the connection rather than in ``.env``: ``.env`` holds
non-secret *service endpoints* (the runner URL), and a model id is solve policy.
Keeping it on the row also means a future per-arena override is a data change
instead of another migration.

All three columns are nullable or defaulted, so every existing row keeps working
unchanged: a connection with no default simply cannot start a solve until one is
set, which is the honest state rather than a silent fallback to an arbitrary
model.  Inspector-guarded so the migration replays safely.
"""

import sqlalchemy as sa

from alembic import op

revision = "0047_arena_default_model"
down_revision = "0046_arena_integration"
branch_labels = None
depends_on = None

TABLE = "arena_connections"
COLUMNS = {
    "default_model_config_id": sa.Column("default_model_config_id", sa.String(length=36), nullable=True),
    "default_engine_type": sa.Column("default_engine_type", sa.String(length=40), nullable=True),
    "default_solver_mode": sa.Column(
        "default_solver_mode", sa.String(length=30), nullable=False, server_default="muteki"
    ),
}


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    existing = _columns(TABLE)
    for name, column in COLUMNS.items():
        if name not in existing:
            op.add_column(TABLE, column)
    # The model reference is a real constraint, but it is added separately so a
    # database that already rolled back mid-column keeps a replayable path.
    inspector = sa.inspect(op.get_bind())
    if TABLE in inspector.get_table_names():
        foreign_keys = {fk.get("constrained_columns", [None])[0] for fk in inspector.get_foreign_keys(TABLE)}
        if "default_model_config_id" not in foreign_keys:
            op.create_foreign_key(
                "fk_arena_connection_default_model",
                TABLE,
                "model_configs",
                ["default_model_config_id"],
                ["id"],
            )


def downgrade() -> None:
    existing = _columns(TABLE)
    if "default_model_config_id" in existing:
        try:
            op.drop_constraint("fk_arena_connection_default_model", TABLE, type_="foreignkey")
        except Exception:  # noqa: BLE001 - the constraint may not exist on a partial replay
            pass
    for name in COLUMNS:
        if name in existing:
            op.drop_column(TABLE, name)
