"""Add durable attack strategy memory to solver state."""

import sqlalchemy as sa
from alembic import op


revision = "0041_attack_strategy_memory"
down_revision = "0040_security_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "solver_states" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("solver_states")}
    if "attack_strategy_history_json" not in columns:
        op.add_column(
            "solver_states",
            sa.Column("attack_strategy_history_json", sa.JSON(), nullable=False, server_default=sa.text("('[]')")),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "solver_states" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("solver_states")}
    if "attack_strategy_history_json" in columns:
        op.drop_column("solver_states", "attack_strategy_history_json")
