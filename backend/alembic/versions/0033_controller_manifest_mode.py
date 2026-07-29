"""Record controller-owned tool-loop mode on Attempt manifests."""

import sqlalchemy as sa
from alembic import op


revision = "0033_controller_manifest_mode"
down_revision = "0032_controller_tool_loop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("attempt_tool_manifests")}
    if "execution_mode" not in columns:
        op.add_column("attempt_tool_manifests", sa.Column("execution_mode", sa.String(length=40), nullable=True))
    op.execute(sa.text("UPDATE attempt_tool_manifests SET execution_mode = 'controller_tool_loop' WHERE execution_mode IS NULL"))
    op.alter_column("attempt_tool_manifests", "execution_mode", existing_type=sa.String(length=40), existing_nullable=True, nullable=False, server_default="controller_tool_loop")
    if "mcp_required" not in columns:
        op.add_column("attempt_tool_manifests", sa.Column("mcp_required", sa.Boolean(), nullable=True))
    op.execute(sa.text("UPDATE attempt_tool_manifests SET mcp_required = 0 WHERE mcp_required IS NULL"))
    op.alter_column("attempt_tool_manifests", "mcp_required", existing_type=sa.Boolean(), existing_nullable=True, nullable=False, server_default=sa.text("0"))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("attempt_tool_manifests")}
    if "mcp_required" in columns:
        op.drop_column("attempt_tool_manifests", "mcp_required")
    if "execution_mode" in columns:
        op.drop_column("attempt_tool_manifests", "execution_mode")
