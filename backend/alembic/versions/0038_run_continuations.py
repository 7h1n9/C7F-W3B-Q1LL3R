"""Persist controller continuations across process boundaries."""

import sqlalchemy as sa
from alembic import op


revision = "0038_run_continuations"
down_revision = "0037_run_report_payload"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "run_continuations" in inspector.get_table_names():
        return
    op.create_table(
        "run_continuations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=True),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="PENDING"),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("owner_instance_id", sa.String(length=120), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["solve_runs.id"]),
        sa.ForeignKeyConstraint(["attempt_id"], ["run_attempts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "dedupe_key", name="uq_run_continuation_dedupe"),
    )
    op.create_index("ix_run_continuations_run_id", "run_continuations", ["run_id"])
    op.create_index("ix_run_continuations_status", "run_continuations", ["status"])
    op.create_index("ix_run_continuations_kind", "run_continuations", ["kind"])
    op.create_index("ix_run_continuations_attempt_id", "run_continuations", ["attempt_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "run_continuations" in inspector.get_table_names():
        op.drop_table("run_continuations")
