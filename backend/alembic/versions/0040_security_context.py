"""Add the compatible security reasoning blackboard to solver state."""

import sqlalchemy as sa
from alembic import op


revision = "0040_security_context"
down_revision = "0039_run_continuation_updated_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "solver_states" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("solver_states")}
    if "security_context_json" not in columns:
        op.add_column(
            "solver_states",
            sa.Column(
                "security_context_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("('{}')"),
            ),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "solver_states" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("solver_states")}
    if "security_context_json" in columns:
        op.drop_column("solver_states", "security_context_json")
