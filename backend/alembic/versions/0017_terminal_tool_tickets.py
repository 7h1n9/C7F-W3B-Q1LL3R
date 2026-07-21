"""Persist terminal-generation guards, logical calls, traces and tool tickets."""

import sqlalchemy as sa
from alembic import op

revision = "0017_terminal_tool_tickets"
down_revision = "0016_multi_chain_solver_controls"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    tables = _tables()
    if "solve_runs" in tables:
        for name, kind, default in (
            ("terminal_generation", sa.Integer(), "0"),
            ("terminal_event_sequence", sa.Integer(), None),
            ("thread_invalidated", sa.Boolean(), "0"),
            ("post_terminal_events_json", sa.JSON(), "[]"),
            ("fresh_reproduction_verified", sa.Boolean(), "0"),
        ):
            if name not in _columns("solve_runs"):
                if name == "post_terminal_events_json":
                    op.add_column("solve_runs", sa.Column(name, kind, nullable=True))
                    op.execute(sa.text("UPDATE solve_runs SET post_terminal_events_json = :empty WHERE post_terminal_events_json IS NULL").bindparams(empty="[]"))
                else:
                    op.add_column("solve_runs", sa.Column(name, kind, nullable=name == "terminal_event_sequence", server_default=default))
    if "artifacts" in tables and "status" not in _columns("artifacts"):
        op.add_column("artifacts", sa.Column("status", sa.String(20), nullable=False, server_default="ACTIVE"))
    if "solver_states" in tables:
        for name in ("investigation_no_progress_count", "duplicate_action_streak", "control_rejection_streak", "schema_error_streak", "degraded_action_streak"):
            if name not in _columns("solver_states"):
                op.add_column("solver_states", sa.Column(name, sa.Integer(), nullable=False, server_default="0"))
    if "logical_tool_calls" not in tables:
        op.create_table(
            "logical_tool_calls",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("run_id", sa.String(36), sa.ForeignKey("solve_runs.id"), nullable=False),
            sa.Column("attempt_id", sa.String(36), sa.ForeignKey("run_attempts.id")),
            sa.Column("engine_type", sa.String(40), nullable=False, server_default="unknown"),
            sa.Column("tool_name", sa.String(100), nullable=False),
            sa.Column("arguments_digest", sa.String(64), nullable=False, server_default=""),
            sa.Column("status", sa.String(40), nullable=False, server_default="REQUESTED"),
            sa.Column("started_at", sa.DateTime(timezone=True)),
            sa.Column("finished_at", sa.DateTime(timezone=True)),
            sa.Column("result_observation_id", sa.String(36), sa.ForeignKey("observations.id")),
            sa.UniqueConstraint("run_id", "id", name="uq_logical_tool_call_run_id"),
        )
    if "tool_execution_traces" not in tables:
        op.create_table(
            "tool_execution_traces",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("logical_tool_call_id", sa.String(36), sa.ForeignKey("logical_tool_calls.id"), nullable=False),
            sa.Column("execution_layer", sa.String(40), nullable=False),
            sa.Column("event_type", sa.String(80), nullable=False),
            sa.Column("external_id", sa.String(255)),
            sa.Column("payload_digest", sa.String(64), nullable=False, server_default=""),
        )
    if "tool_invocation_tickets" not in tables:
        op.create_table(
            "tool_invocation_tickets",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("ticket_hash", sa.String(128), nullable=False),
            sa.Column("run_id", sa.String(36), sa.ForeignKey("solve_runs.id"), nullable=False),
            sa.Column("attempt_id", sa.String(36), sa.ForeignKey("run_attempts.id"), nullable=False),
            sa.Column("thread_id", sa.String(255)),
            sa.Column("model_turn_id", sa.String(255)),
            sa.Column("lease_id", sa.String(36), sa.ForeignKey("run_execution_leases.id"), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("ticket_hash", name="uq_tool_invocation_ticket_hash"),
        )


def downgrade() -> None:
    for table in ("tool_invocation_tickets", "tool_execution_traces", "logical_tool_calls"):
        if table in _tables():
            op.drop_table(table)
    if "artifacts" in _tables() and "status" in _columns("artifacts"):
        op.drop_column("artifacts", "status")
    if "solve_runs" in _tables():
        for name in ("fresh_reproduction_verified", "post_terminal_events_json", "thread_invalidated", "terminal_event_sequence", "terminal_generation"):
            if name in _columns("solve_runs"):
                op.drop_column("solve_runs", name)
    if "solver_states" in _tables():
        for name in ("degraded_action_streak", "schema_error_streak", "control_rejection_streak", "duplicate_action_streak", "investigation_no_progress_count"):
            if name in _columns("solver_states"):
                op.drop_column("solver_states", name)
