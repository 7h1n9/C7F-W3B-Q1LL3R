"""Persist the complete WP/report payload on solve_runs."""

import sqlalchemy as sa
from alembic import op


revision = "0037_run_report_payload"
down_revision = "0036_asset_warranty_mysql_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "report_json" not in {column["name"] for column in inspector.get_columns("solve_runs")}:
        op.add_column("solve_runs", sa.Column("report_json", sa.JSON(), nullable=True))
        op.execute(sa.text("UPDATE solve_runs SET report_json = '{}' WHERE report_json IS NULL"))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "report_json" in {column["name"] for column in inspector.get_columns("solve_runs")}:
        op.drop_column("solve_runs", "report_json")
