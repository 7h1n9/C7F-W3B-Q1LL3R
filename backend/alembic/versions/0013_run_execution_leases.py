"""Add run execution leases and attempt heartbeats."""
import sqlalchemy as sa

from alembic import op

revision = "0013_run_execution_leases"
down_revision = "0012_run_attempts"
branch_labels = None
depends_on = None


def _column_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    columns = _column_names("run_attempts")
    if "initial_agent_steps" not in columns:
        op.add_column("run_attempts", sa.Column("initial_agent_steps", sa.Integer(), nullable=False, server_default="0"))
    if "initial_tool_calls" not in columns:
        op.add_column("run_attempts", sa.Column("initial_tool_calls", sa.Integer(), nullable=False, server_default="0"))
    if "initial_input_tokens" not in columns:
        op.add_column("run_attempts", sa.Column("initial_input_tokens", sa.Integer(), nullable=False, server_default="0"))
    if "initial_output_tokens" not in columns:
        op.add_column("run_attempts", sa.Column("initial_output_tokens", sa.Integer(), nullable=False, server_default="0"))
    if "heartbeat_at" not in columns:
        op.add_column("run_attempts", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))

    if "run_execution_leases" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "run_execution_leases",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("run_id", sa.String(36), sa.ForeignKey("solve_runs.id"), nullable=False),
            sa.Column("attempt_id", sa.String(36), sa.ForeignKey("run_attempts.id"), nullable=False),
            sa.Column("owner_instance_id", sa.String(120), nullable=False),
            sa.Column("lease_token", sa.String(120), nullable=False),
            sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("run_id", name="uq_run_execution_lease_run"),
            sa.UniqueConstraint("lease_token", name="uq_run_execution_lease_token"),
        )
        op.create_index("ix_run_execution_leases_run_id", "run_execution_leases", ["run_id"])


def downgrade() -> None:
    if "run_execution_leases" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_index("ix_run_execution_leases_run_id", table_name="run_execution_leases")
        op.drop_table("run_execution_leases")
    columns = _column_names("run_attempts")
    for name in (
        "heartbeat_at",
        "initial_output_tokens",
        "initial_input_tokens",
        "initial_tool_calls",
        "initial_agent_steps",
    ):
        if name in columns:
            op.drop_column("run_attempts", name)

