"""Record Runner network enforcement capabilities in each Attempt manifest."""

import sqlalchemy as sa
from alembic import op


revision = "0029_network_enforcement_manifest"
down_revision = "0028_script_execution_contract"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {item["name"] for item in inspector.get_columns("attempt_tool_manifests")}
    if "network_enforcement_json" not in columns:
        op.add_column("attempt_tool_manifests", sa.Column("network_enforcement_json", sa.JSON(), nullable=True))
    if "tool_capabilities_json" not in columns:
        op.add_column("attempt_tool_manifests", sa.Column("tool_capabilities_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {item["name"] for item in inspector.get_columns("attempt_tool_manifests")}
    for name in ("tool_capabilities_json", "network_enforcement_json"):
        if name in columns:
            op.drop_column("attempt_tool_manifests", name)
