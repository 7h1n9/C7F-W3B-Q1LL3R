"""Persist generated-script provenance separately from execution artifacts."""

import sqlalchemy as sa

from alembic import op

revision = "0020_script_provenance"
down_revision = "0019_incremental_compaction_and_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "scripts" in tables:
        return
    op.create_table(
        "scripts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("solve_runs.id"), nullable=False),
        sa.Column("artifact_id", sa.String(36), sa.ForeignKey("artifacts.id")),
        sa.Column("path", sa.String(1024), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False, server_default=""),
        sa.Column("source", sa.String(30), nullable=False, server_default="MODEL_GENERATED"),
        sa.Column("assistance_level", sa.String(30), nullable=False, server_default="AUTONOMOUS"),
        sa.Column("assumption_provenance_json", sa.JSON(), nullable=False),
        sa.Column("design_card_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="ACTIVE"),
    )
    op.create_index("ix_scripts_run_id", "scripts", ["run_id"])


def downgrade() -> None:
    if "scripts" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("scripts")
