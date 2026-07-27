"""Persist the only authorized challenge methodology hints."""

import sqlalchemy as sa

from alembic import op

revision = "0022_durable_methodology_hints"
down_revision = "0021_methodology_43_contracts"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if "hints_json" not in _columns("solve_runs"):
        op.add_column("solve_runs", sa.Column("hints_json", sa.JSON(), nullable=True))
    if "hints_json" not in _columns("run_attempts"):
        op.add_column("run_attempts", sa.Column("hints_json", sa.JSON(), nullable=True))
    op.execute(sa.text("UPDATE solve_runs SET hints_json = '{}' WHERE hints_json IS NULL"))
    op.execute(sa.text("UPDATE run_attempts SET hints_json = (SELECT hints_json FROM solve_runs WHERE solve_runs.id = run_attempts.run_id) WHERE hints_json IS NULL"))


def downgrade() -> None:
    if "hints_json" in _columns("run_attempts"):
        op.drop_column("run_attempts", "hints_json")
    if "hints_json" in _columns("solve_runs"):
        op.drop_column("solve_runs", "hints_json")
