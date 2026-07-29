"""Durable controller-owned role actions and approved tool capabilities."""

import sqlalchemy as sa
from alembic import op


revision = "0032_controller_tool_loop"
down_revision = "0031_role_agent_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    def columns(table: str) -> set[str]:
        return {item["name"] for item in sa.inspect(bind).get_columns(table)}

    def add(table: str, column: sa.Column) -> None:
        if column.name not in columns(table):
            op.add_column(table, column)

    add("agent_tasks", sa.Column("total_deadline_at", sa.DateTime(timezone=True), nullable=True))
    add("agent_tasks", sa.Column("idle_deadline_at", sa.DateTime(timezone=True), nullable=True))
    add("agent_tasks", sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True))
    add("agent_tasks", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    add("tool_calls", sa.Column("approved_action_id", sa.String(length=36), nullable=True))
    add("tool_calls", sa.Column("agent_role", sa.String(length=30), nullable=True))
    add("tool_calls", sa.Column("task_lease_token", sa.String(length=120), nullable=True))
    add("analysis_reviews", sa.Column("approved_fact_indexes_json", sa.JSON(), nullable=True))
    add("analysis_reviews", sa.Column("approved_evidence_ids_json", sa.JSON(), nullable=True))
    add("analysis_reviews", sa.Column("approved_hypothesis_updates_json", sa.JSON(), nullable=True))
    add("analysis_reviews", sa.Column("capabilities_added_json", sa.JSON(), nullable=True))
    add("analysis_reviews", sa.Column("solution_step_accepted", sa.Boolean(), nullable=True))
    add("analysis_reviews", sa.Column("next_phase", sa.String(length=80), nullable=True))

    if "approved_actions" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "approved_actions",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("run_id", sa.String(length=36), sa.ForeignKey("solve_runs.id"), nullable=False),
            sa.Column("approved_action_id", sa.String(length=80), nullable=False),
            sa.Column("proposal_id", sa.String(length=36), sa.ForeignKey("planner_proposals.id"), nullable=False),
            sa.Column("analysis_review_id", sa.String(length=36), sa.ForeignKey("analysis_reviews.id"), nullable=False),
            sa.Column("agent_role", sa.String(length=30), nullable=False),
            sa.Column("tool_name", sa.String(length=100), nullable=False),
            sa.Column("argument_constraints_json", sa.JSON(), nullable=False),
            sa.Column("max_logical_calls", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("used_logical_calls", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="ACTIVE"),
            sa.UniqueConstraint("run_id", "approved_action_id", name="uq_approved_action_run_id"),
        )
    op.execute(sa.text("UPDATE analysis_reviews SET approved_fact_indexes_json = '[]' WHERE approved_fact_indexes_json IS NULL"))
    op.execute(sa.text("UPDATE analysis_reviews SET approved_evidence_ids_json = '[]' WHERE approved_evidence_ids_json IS NULL"))
    op.execute(sa.text("UPDATE analysis_reviews SET approved_hypothesis_updates_json = '[]' WHERE approved_hypothesis_updates_json IS NULL"))
    op.execute(sa.text("UPDATE analysis_reviews SET capabilities_added_json = '[]' WHERE capabilities_added_json IS NULL"))
    op.execute(sa.text("UPDATE analysis_reviews SET solution_step_accepted = 0 WHERE solution_step_accepted IS NULL"))
    op.execute(sa.text("UPDATE analysis_reviews SET next_phase = 'HYPOTHESIS' WHERE next_phase IS NULL"))
    indexes = {item.get("name") for item in sa.inspect(bind).get_indexes("tool_calls")}
    if "ix_tool_calls_approved_action_id" not in indexes:
        op.create_index("ix_tool_calls_approved_action_id", "tool_calls", ["approved_action_id"])
    if "ix_approved_actions_run_id" not in {item.get("name") for item in sa.inspect(bind).get_indexes("approved_actions")}:
        op.create_index("ix_approved_actions_run_id", "approved_actions", ["run_id"])
    if "ix_approved_actions_status" not in {item.get("name") for item in sa.inspect(bind).get_indexes("approved_actions")}:
        op.create_index("ix_approved_actions_status", "approved_actions", ["status"])


def downgrade() -> None:
    bind = op.get_bind()
    if "ix_tool_calls_approved_action_id" in {item.get("name") for item in sa.inspect(bind).get_indexes("tool_calls")}:
        op.drop_index("ix_tool_calls_approved_action_id", table_name="tool_calls")
    if "approved_actions" in sa.inspect(bind).get_table_names():
        op.drop_table("approved_actions")
    for name in ("task_lease_token", "agent_role", "approved_action_id"):
        if name in {item["name"] for item in sa.inspect(bind).get_columns("tool_calls")}:
            op.drop_column("tool_calls", name)
    for name in ("heartbeat_at", "last_activity_at", "idle_deadline_at", "total_deadline_at"):
        if name in {item["name"] for item in sa.inspect(bind).get_columns("agent_tasks")}:
            op.drop_column("agent_tasks", name)
