"""Persist Codex recovery checkpoints and per-attempt tool manifests."""

import sqlalchemy as sa

from alembic import op

revision = "0026_codex_recovery_chain"
down_revision = "0025_data_governance_web_research"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {item["name"] for item in inspector.get_columns("solve_runs")}
    if "recovery_checkpoint_json" not in columns:
        op.add_column("solve_runs", sa.Column("recovery_checkpoint_json", sa.JSON(), nullable=False))
    if "workspace_revision" not in columns:
        op.add_column("solve_runs", sa.Column("workspace_revision", sa.Integer(), nullable=False, server_default="0"))
    if "workspace_negative_cache_json" not in columns:
        op.add_column("solve_runs", sa.Column("workspace_negative_cache_json", sa.JSON(), nullable=False))
    if "attempt_tool_manifests" not in set(inspector.get_table_names()):
        op.create_table(
            "attempt_tool_manifests",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("run_id", sa.String(36), sa.ForeignKey("solve_runs.id"), nullable=False),
            sa.Column("attempt_id", sa.String(36), sa.ForeignKey("run_attempts.id"), nullable=False),
            sa.Column("role_snapshot_tools", sa.JSON(), nullable=False),
            sa.Column("challenge_allowed_tools", sa.JSON(), nullable=False),
            sa.Column("backend_registry_tools", sa.JSON(), nullable=False),
            sa.Column("runner_capability_tools", sa.JSON(), nullable=False),
            sa.Column("mcp_advertised_tools", sa.JSON(), nullable=False),
            sa.Column("effective_tools", sa.JSON(), nullable=False),
            sa.Column("missing_expected_tools", sa.JSON(), nullable=False),
            sa.Column("schema_hashes", sa.JSON(), nullable=False),
            sa.Column("manifest_sha256", sa.String(64), nullable=False),
            sa.UniqueConstraint("attempt_id", name="uq_attempt_tool_manifest_attempt"),
        )
        op.create_index("ix_attempt_tool_manifests_run_id", "attempt_tool_manifests", ["run_id"])
        op.create_index("ix_attempt_tool_manifests_attempt_id", "attempt_tool_manifests", ["attempt_id"])


def downgrade() -> None:
    if "attempt_tool_manifests" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("attempt_tool_manifests")
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("solve_runs")}
    if "recovery_checkpoint_json" in columns:
        op.drop_column("solve_runs", "recovery_checkpoint_json")
    if "workspace_revision" in columns:
        op.drop_column("solve_runs", "workspace_revision")
    if "workspace_negative_cache_json" in columns:
        op.drop_column("solve_runs", "workspace_negative_cache_json")
