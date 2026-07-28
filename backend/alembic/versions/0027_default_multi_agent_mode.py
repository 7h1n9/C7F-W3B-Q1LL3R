"""Make multi_agent_v1 the database default for newly created Runs."""

import sqlalchemy as sa

from alembic import op


revision = "0027_default_multi_agent_mode"
down_revision = "0026_codex_recovery_chain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {item["name"] for item in inspector.get_columns("solve_runs")}
    if "solver_mode" in columns:
        op.alter_column(
            "solve_runs",
            "solver_mode",
            existing_type=sa.String(30),
            existing_nullable=False,
            server_default=sa.text("'multi_agent_v1'"),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {item["name"] for item in inspector.get_columns("solve_runs")}
    if "solver_mode" in columns:
        op.alter_column(
            "solve_runs",
            "solver_mode",
            existing_type=sa.String(30),
            existing_nullable=False,
            server_default=sa.text("'single_agent'"),
        )
