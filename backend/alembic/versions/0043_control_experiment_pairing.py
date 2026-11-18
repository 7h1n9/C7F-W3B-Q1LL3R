"""Enforce one control Run per source Run."""

import sqlalchemy as sa
from alembic import op


revision = "0043_control_experiment_pairing"
down_revision = "0042_solver_scoring"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "solve_runs" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("solve_runs")}
    if "control_source_run_id" not in columns:
        op.add_column(
            "solve_runs",
            sa.Column("control_source_run_id", sa.String(length=36), nullable=True),
        )

    inspector = sa.inspect(op.get_bind())
    unique_names = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("solve_runs")
    }
    if "uq_solve_run_control_source_run" not in unique_names:
        op.create_unique_constraint(
            "uq_solve_run_control_source_run",
            "solve_runs",
            ["control_source_run_id"],
        )

    inspector = sa.inspect(op.get_bind())
    foreign_key_names = {
        constraint["name"]
        for constraint in inspector.get_foreign_keys("solve_runs")
    }
    if "fk_solve_runs_control_source_run" not in foreign_key_names:
        op.create_foreign_key(
            "fk_solve_runs_control_source_run",
            "solve_runs",
            "solve_runs",
            ["control_source_run_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "solve_runs" not in inspector.get_table_names():
        return

    foreign_key_names = {
        constraint["name"]
        for constraint in inspector.get_foreign_keys("solve_runs")
    }
    if "fk_solve_runs_control_source_run" in foreign_key_names:
        op.drop_constraint(
            "fk_solve_runs_control_source_run",
            "solve_runs",
            type_="foreignkey",
        )

    inspector = sa.inspect(op.get_bind())
    unique_names = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("solve_runs")
    }
    if "uq_solve_run_control_source_run" in unique_names:
        op.drop_constraint(
            "uq_solve_run_control_source_run",
            "solve_runs",
            type_="unique",
        )

    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("solve_runs")}
    if "control_source_run_id" in columns:
        op.drop_column("solve_runs", "control_source_run_id")
