"""Add recovery controls, checkpoints, and queued runtime user inputs."""

from alembic import op
import sqlalchemy as sa

revision = "0014_solver_recovery_controls"
down_revision = "0013_run_execution_leases"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {item["name"] for item in inspector.get_columns(table)} if table in inspector.get_table_names() else set()


def upgrade() -> None:
    columns = _columns("solve_runs")
    for name, kind, default in (
        ("agent_checkpoint_interval", sa.Integer(), "30"),
        ("context_revision", sa.Integer(), "0"),
        ("infrastructure_retry_count", sa.Integer(), "0"),
    ):
        if name not in columns:
            op.add_column("solve_runs", sa.Column(name, kind, nullable=False, server_default=default))
    if "run_user_inputs" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "run_user_inputs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("run_id", sa.String(36), sa.ForeignKey("solve_runs.id"), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("input_type", sa.String(40), nullable=False, server_default="SUPPLEMENT"),
            sa.Column("status", sa.String(20), nullable=False, server_default="QUEUED"),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("consumed_by_attempt_id", sa.String(36), sa.ForeignKey("run_attempts.id"), nullable=True),
        )
        op.create_index("ix_run_user_inputs_run_id", "run_user_inputs", ["run_id"])
        op.create_index("ix_run_user_inputs_status", "run_user_inputs", ["status"])


def downgrade() -> None:
    if "run_user_inputs" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_index("ix_run_user_inputs_status", table_name="run_user_inputs")
        op.drop_index("ix_run_user_inputs_run_id", table_name="run_user_inputs")
        op.drop_table("run_user_inputs")
    columns = _columns("solve_runs")
    for name in ("infrastructure_retry_count", "context_revision", "agent_checkpoint_interval"):
        if name in columns:
            op.drop_column("solve_runs", name)
