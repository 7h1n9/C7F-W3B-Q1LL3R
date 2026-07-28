"""Persist role runtime context and model-turn ownership."""

import sqlalchemy as sa
from alembic import op


revision = "0031_role_agent_runtime"
down_revision = "0030_run_phase_contract"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    def add(table: str, column: sa.Column) -> None:
        if column.name not in {item["name"] for item in inspector.get_columns(table)}:
            op.add_column(table, column)

    add("agent_tasks", sa.Column("task_kind", sa.String(length=30), nullable=False, server_default="RECON"))
    add("agent_tasks", sa.Column("context_json", sa.JSON(), nullable=True))
    # TEXT/JSON defaults are rejected by MySQL.  Add nullable, backfill, then
    # tighten the columns below.
    add("planner_proposals", sa.Column("decision_question", sa.Text(), nullable=True))
    add("analysis_reviews", sa.Column("task_kind", sa.String(length=30), nullable=False, server_default="PLAN_REVIEW"))
    add("analysis_reviews", sa.Column("approved_arguments_json", sa.JSON(), nullable=True))
    add("agent_turns", sa.Column("agent_task_id", sa.String(length=36), nullable=True))
    add("agent_turns", sa.Column("agent_role", sa.String(length=30), nullable=True))
    add("tool_calls", sa.Column("agent_task_id", sa.String(length=36), nullable=True))
    existing_indexes = {item.get("name") for item in inspector.get_indexes("agent_turns")}
    if "ix_agent_turns_agent_task_id" not in existing_indexes:
        op.create_index("ix_agent_turns_agent_task_id", "agent_turns", ["agent_task_id"])
    if "ix_agent_turns_agent_role" not in existing_indexes:
        op.create_index("ix_agent_turns_agent_role", "agent_turns", ["agent_role"])
    if "ix_tool_calls_agent_task_id" not in {item.get("name") for item in inspector.get_indexes("tool_calls")}:
        op.create_index("ix_tool_calls_agent_task_id", "tool_calls", ["agent_task_id"])
    foreign_keys = {(item.get("name"), item.get("constrained_columns", [None])[0]) for item in inspector.get_foreign_keys("agent_turns")}
    if ("fk_agent_turns_agent_task_id", "agent_task_id") not in foreign_keys:
        op.create_foreign_key("fk_agent_turns_agent_task_id", "agent_turns", "agent_tasks", ["agent_task_id"], ["id"])
    foreign_keys = {(item.get("name"), item.get("constrained_columns", [None])[0]) for item in inspector.get_foreign_keys("tool_calls")}
    if ("fk_tool_calls_agent_task_id", "agent_task_id") not in foreign_keys:
        op.create_foreign_key("fk_tool_calls_agent_task_id", "tool_calls", "agent_tasks", ["agent_task_id"], ["id"])
    op.execute(sa.text("UPDATE agent_tasks SET context_json = '{}' WHERE context_json IS NULL"))
    op.execute(sa.text("UPDATE analysis_reviews SET approved_arguments_json = '{}' WHERE approved_arguments_json IS NULL"))
    op.execute(sa.text("UPDATE planner_proposals SET decision_question = '' WHERE decision_question IS NULL"))
    op.alter_column("agent_tasks", "context_json", existing_type=sa.JSON(), nullable=False)
    op.alter_column("analysis_reviews", "approved_arguments_json", existing_type=sa.JSON(), nullable=False)
    op.alter_column("planner_proposals", "decision_question", existing_type=sa.Text(), nullable=False)
    inspector = sa.inspect(op.get_bind())
    unique_names = {item.get("name") for item in inspector.get_unique_constraints("analysis_reviews")}
    for name in ("proposal_id", "uq_analysis_reviews_proposal_id"):
        if name in unique_names:
            review_fk = next((item.get("name") for item in inspector.get_foreign_keys("analysis_reviews") if item.get("constrained_columns") == ["proposal_id"]), None)
            if review_fk:
                op.drop_constraint(review_fk, "analysis_reviews", type_="foreignkey")
            op.drop_constraint(name, "analysis_reviews", type_="unique")
            op.create_foreign_key(review_fk or "fk_analysis_reviews_proposal_id", "analysis_reviews", "planner_proposals", ["proposal_id"], ["id"])
            break
    op.create_unique_constraint("uq_analysis_review_kind", "analysis_reviews", ["proposal_id", "task_kind"])


def downgrade() -> None:
    op.drop_constraint("fk_tool_calls_agent_task_id", "tool_calls", type_="foreignkey")
    op.drop_constraint("fk_agent_turns_agent_task_id", "agent_turns", type_="foreignkey")
    op.drop_index("ix_tool_calls_agent_task_id", table_name="tool_calls")
    op.drop_constraint("uq_analysis_review_kind", "analysis_reviews", type_="unique")
    op.drop_index("ix_agent_turns_agent_role", table_name="agent_turns")
    op.drop_index("ix_agent_turns_agent_task_id", table_name="agent_turns")
    op.drop_column("agent_turns", "agent_role")
    op.drop_column("agent_turns", "agent_task_id")
    op.drop_column("analysis_reviews", "approved_arguments_json")
    op.drop_column("analysis_reviews", "task_kind")
    op.drop_column("planner_proposals", "decision_question")
    op.drop_column("agent_tasks", "context_json")
    op.drop_column("agent_tasks", "task_kind")
