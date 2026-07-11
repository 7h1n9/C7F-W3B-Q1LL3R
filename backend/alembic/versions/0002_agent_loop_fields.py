"""add durable single-agent solve loop fields

Revision ID: 0002_agent_loop
Revises: 0001_initial
Create Date: 2026-07-11
"""
import sqlalchemy as sa

from alembic import op

revision = "0002_agent_loop"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("solve_runs") as batch:
        batch.add_column(sa.Column("max_agent_steps", sa.Integer(), nullable=False, server_default="12"))
        batch.add_column(sa.Column("max_tool_calls", sa.Integer(), nullable=False, server_default="12"))
        batch.add_column(sa.Column("max_context_observations", sa.Integer(), nullable=False, server_default="8"))
        batch.add_column(sa.Column("max_runtime_seconds", sa.Integer(), nullable=False, server_default="300"))
        batch.add_column(sa.Column("agent_step_count", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("tool_call_count", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("event_sequence", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("last_error_code", sa.String(100)))
        batch.add_column(sa.Column("last_error_message", sa.Text()))
    op.create_index("ix_tool_calls_run_created", "tool_calls", ["run_id", "created_at"])
    op.create_index("ix_run_events_run_sequence", "run_events", ["run_id", "sequence"])


def downgrade() -> None:
    op.drop_index("ix_run_events_run_sequence", table_name="run_events")
    op.drop_index("ix_tool_calls_run_created", table_name="tool_calls")
    with op.batch_alter_table("solve_runs") as batch:
        for name in ("last_error_message", "last_error_code", "event_sequence", "tool_call_count", "agent_step_count", "max_runtime_seconds", "max_context_observations", "max_tool_calls", "max_agent_steps"):
            batch.drop_column(name)
