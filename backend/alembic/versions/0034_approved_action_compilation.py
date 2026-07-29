"""Persist controller-compiled ApprovedAction arguments and provenance."""

import sqlalchemy as sa
from alembic import op


revision = "0034_approved_action_compilation"
down_revision = "0033_controller_manifest_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("approved_actions")}
    additions = [
        sa.Column("compiled_arguments_json", sa.JSON(), nullable=True),
        sa.Column("compiled_arguments_digest", sa.String(length=64), nullable=True),
        sa.Column("tool_schema_hash", sa.String(length=64), nullable=True),
        sa.Column("compiler_name", sa.String(length=120), nullable=True),
        sa.Column("compiler_version", sa.String(length=40), nullable=True),
        sa.Column("compile_status", sa.String(length=30), nullable=True),
        sa.Column("compile_error_json", sa.JSON(), nullable=True),
    ]
    for column in additions:
        if column.name not in columns:
            op.add_column("approved_actions", column)
    op.execute(sa.text("UPDATE approved_actions SET compile_status = 'PENDING_COMPILE' WHERE compile_status IS NULL"))
    op.alter_column("approved_actions", "compile_status", existing_type=sa.String(length=30), nullable=False, server_default="PENDING_COMPILE")
    if "ix_approved_actions_compile_status" not in {item.get("name") for item in sa.inspect(bind).get_indexes("approved_actions")}:
        op.create_index("ix_approved_actions_compile_status", "approved_actions", ["compile_status"])


def downgrade() -> None:
    bind = op.get_bind()
    if "ix_approved_actions_compile_status" in {item.get("name") for item in sa.inspect(bind).get_indexes("approved_actions")}:
        op.drop_index("ix_approved_actions_compile_status", table_name="approved_actions")
    for name in ("compile_error_json", "compile_status", "compiler_version", "compiler_name", "tool_schema_hash", "compiled_arguments_digest", "compiled_arguments_json"):
        if name in {item["name"] for item in sa.inspect(bind).get_columns("approved_actions")}:
            op.drop_column("approved_actions", name)
