"""Add the first durable multi-agent core loop.

The migration is additive and keeps the existing single-agent run path
usable.  All agent output is stored as bounded JSON summaries, never as a
full conversation transcript.
"""

import sqlalchemy as sa

from alembic import op


revision = "0024_multi_agent_core"
down_revision = "0023_compaction_lease_updated_at"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def _json(name: str, default: str = "{}") -> sa.Column:
    # MySQL 8 rejects string defaults on JSON columns. ORM defaults supply
    # empty containers for new rows, so schema creation needs no DB default.
    return sa.Column(name, sa.JSON(), nullable=False)


def _text(name: str) -> sa.Column:
    # MySQL does not allow defaults on TEXT columns.
    return sa.Column(name, sa.Text(), nullable=False)


def upgrade() -> None:
    tables = _tables()
    if "solve_runs" in tables:
        columns = _columns("solve_runs")
        for name, kind, default in (
            ("solver_mode", sa.String(30), "single_agent"),
            ("controller_revision", sa.Integer(), "0"),
        ):
            if name not in columns:
                op.add_column("solve_runs", sa.Column(name, kind, nullable=False, server_default=default))

    if "agent_role_policies" not in tables:
        op.create_table(
            "agent_role_policies",
            sa.Column("id", sa.String(36), primary_key=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("role", sa.String(30), nullable=False), _text("system_prompt"),
            _json("readable_memory_types_json", "[]"), _json("allowed_tool_types_json", "[]"), _json("allowed_tools_json", "[]"),
            _json("allowed_outputs_json", "[]"), _json("forbidden_operations_json", "[]"),
            sa.Column("max_logical_calls", sa.Integer(), nullable=False, server_default="1"), sa.Column("max_internal_requests", sa.Integer(), nullable=False, server_default="8"),
            sa.Column("max_runtime_seconds", sa.Integer(), nullable=False, server_default="120"), sa.Column("default_timeout_seconds", sa.Integer(), nullable=False, server_default="120"),
            sa.Column("max_retries", sa.Integer(), nullable=False, server_default="1"), sa.Column("can_create_candidate_fact", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("can_verify_fact", sa.Boolean(), nullable=False, server_default="0"), sa.Column("can_submit_flag", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("can_change_run_status", sa.Boolean(), nullable=False, server_default="0"), sa.Column("schema_version", sa.String(30), nullable=False, server_default="v1"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"), sa.UniqueConstraint("role", name="uq_agent_role_policy_role"),
        )

    if "agent_tasks" not in tables:
        op.create_table(
            "agent_tasks",
            sa.Column("id", sa.String(36), primary_key=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("run_id", sa.String(36), sa.ForeignKey("solve_runs.id"), nullable=False), sa.Column("agent_role", sa.String(30), nullable=False), sa.Column("objective", sa.Text(), nullable=False),
            _json("known_fact_ids_json", "[]"), _json("active_hypothesis_ids_json", "[]"), _json("allowed_tools_json", "[]"), _json("budget_json", "{}"),
            _text("success_condition"), _json("stop_conditions_json", "[]"), sa.Column("evidence_snapshot_id", sa.String(36)),
            sa.Column("created_by_task_id", sa.String(36), sa.ForeignKey("agent_tasks.id")), sa.Column("status", sa.String(30), nullable=False, server_default="PENDING"),
            sa.Column("lease_owner", sa.String(120)), sa.Column("lease_token", sa.String(120), unique=True), sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
            sa.Column("optimistic_version", sa.Integer(), nullable=False, server_default="0"), sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="120"),
            sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default="0"), sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("input_snapshot_version", sa.Integer(), nullable=False, server_default="0"), sa.Column("result_schema_version", sa.String(30), nullable=False, server_default="v1"),
        )
        op.create_index("ix_agent_tasks_run_id", "agent_tasks", ["run_id"])
        op.create_index("ix_agent_tasks_agent_role", "agent_tasks", ["agent_role"])
        op.create_index("ix_agent_tasks_status", "agent_tasks", ["status"])

    if "agent_task_results" not in tables:
        op.create_table(
            "agent_task_results",
            sa.Column("id", sa.String(36), primary_key=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("task_id", sa.String(36), sa.ForeignKey("agent_tasks.id"), nullable=False, unique=True), sa.Column("status", sa.String(30), nullable=False),
            _json("new_facts_json", "[]"), _json("updated_hypotheses_json", "[]"), _json("evidence_ids_json", "[]"), _json("accepted_solution_steps_json", "[]"), _json("rejected_paths_json", "[]"),
            sa.Column("failure_classification_json", sa.JSON()), _json("proposed_next_action_json", "{}"), _text("handoff_summary"), sa.Column("schema_version", sa.String(30), nullable=False, server_default="v1"),
        )

    if "planner_proposals" not in tables:
        op.create_table(
            "planner_proposals",
            sa.Column("id", sa.String(36), primary_key=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("run_id", sa.String(36), sa.ForeignKey("solve_runs.id"), nullable=False), sa.Column("proposal_id", sa.String(80), nullable=False), sa.Column("current_stage", sa.String(40), nullable=False), sa.Column("next_agent", sa.String(30), nullable=False), sa.Column("objective", sa.Text(), nullable=False),
            _json("input_fact_ids_json", "[]"), _json("required_capabilities_json", "[]"), _json("allowed_tools_json", "[]"), _json("budget_json", "{}"), sa.Column("success_condition", sa.Text(), nullable=False), _json("stop_conditions_json", "[]"),
            sa.Column("fallback", sa.String(80), nullable=False, server_default="RETURN_TO_ANALYSIS"), sa.Column("status", sa.String(30), nullable=False, server_default="PROPOSED"), sa.Column("created_by_task_id", sa.String(36), sa.ForeignKey("agent_tasks.id")),
            sa.UniqueConstraint("run_id", "proposal_id", name="uq_planner_proposal_run_id"),
        )
        op.create_index("ix_planner_proposals_run_id", "planner_proposals", ["run_id"])

    if "analysis_reviews" not in tables:
        op.create_table(
            "analysis_reviews",
            sa.Column("id", sa.String(36), primary_key=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("proposal_id", sa.String(36), sa.ForeignKey("planner_proposals.id"), nullable=False, unique=True), sa.Column("decision", sa.String(30), nullable=False), sa.Column("confidence", sa.Integer(), nullable=False, server_default="0"), _text("question_being_tested"),
            _json("supporting_evidence_ids_json", "[]"), sa.Column("independent_variable", sa.String(255)), _json("required_controls_json", "{}"), _json("expected_true_signal_json", "{}"), _json("expected_false_signal_json", "{}"), sa.Column("recommended_tool", sa.String(100)), _text("reason"), _text("audit_reason"),
        )

    if "verified_facts" not in tables:
        op.create_table(
            "verified_facts",
            sa.Column("id", sa.String(36), primary_key=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True), sa.Column("run_id", sa.String(36), sa.ForeignKey("solve_runs.id"), nullable=False), sa.Column("fact_key", sa.String(255), nullable=False), sa.Column("fact_type", sa.String(80), nullable=False), sa.Column("value_json", sa.JSON()), sa.Column("confidence", sa.Integer(), nullable=False, server_default="0"), _json("evidence_ids_json", "[]"), sa.Column("source_task_id", sa.String(36), sa.ForeignKey("agent_tasks.id")), sa.Column("promotion_status", sa.String(20), nullable=False, server_default="CANDIDATE"), sa.UniqueConstraint("run_id", "fact_key", name="uq_verified_fact_run_key"),
        )
        op.create_index("ix_verified_facts_run_id", "verified_facts", ["run_id"])

    if "evidence_ledger" not in tables:
        op.create_table(
            "evidence_ledger",
            sa.Column("id", sa.String(36), primary_key=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True), sa.Column("evidence_type", sa.String(80), nullable=False), sa.Column("run_id", sa.String(36), sa.ForeignKey("solve_runs.id"), nullable=False), sa.Column("artifact_id", sa.String(36), sa.ForeignKey("artifacts.id")), sa.Column("tool_call_id", sa.String(36), sa.ForeignKey("tool_calls.id")), sa.Column("agent_task_id", sa.String(36), sa.ForeignKey("agent_tasks.id")), _text("summary"), sa.Column("sha256", sa.String(64), nullable=False, server_default=""), sa.Column("status", sa.String(20), nullable=False, server_default="VERIFIED"), sa.Column("retention_class", sa.String(30), nullable=False, server_default="PROTECTED"), _json("source_chain", "[]"),
        )
        op.create_index("ix_evidence_ledger_run_id", "evidence_ledger", ["run_id"])

    if "solution_chain_nodes" not in tables:
        op.create_table(
            "solution_chain_nodes",
            sa.Column("id", sa.String(36), primary_key=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True), sa.Column("run_id", sa.String(36), sa.ForeignKey("solve_runs.id"), nullable=False), sa.Column("node_id", sa.String(80), nullable=False), sa.Column("stage", sa.String(40), nullable=False), sa.Column("objective", sa.Text(), nullable=False), _json("input_fact_ids_json", "[]"), sa.Column("agent_task_id", sa.String(36), sa.ForeignKey("agent_tasks.id"), nullable=False), sa.Column("logical_tool_call_id", sa.String(120)), _json("result_fact_ids_json", "[]"), sa.Column("capability_added", sa.String(255)), _json("evidence_ids_json", "[]"), sa.Column("status", sa.String(20), nullable=False, server_default="CANDIDATE"), sa.UniqueConstraint("run_id", "node_id", name="uq_solution_node_run_id"),
        )
        op.create_index("ix_solution_chain_nodes_run_id", "solution_chain_nodes", ["run_id"])

    if "failure_signatures" not in tables:
        op.create_table(
            "failure_signatures",
            sa.Column("id", sa.String(36), primary_key=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True), sa.Column("run_id", sa.String(36), sa.ForeignKey("solve_runs.id"), nullable=False), sa.Column("fingerprint", sa.String(255), nullable=False), sa.Column("classification", sa.String(80), nullable=False), sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1"), sa.Column("retryable", sa.Boolean(), nullable=False, server_default="0"), sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False), sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False), _text("reason"), _text("next_allowed_condition"), sa.UniqueConstraint("run_id", "fingerprint", name="uq_failure_signature_run_fp"),
        )
        op.create_index("ix_failure_signatures_run_id", "failure_signatures", ["run_id"])

    if "memory_snapshots" not in tables:
        op.create_table(
            "memory_snapshots",
            sa.Column("id", sa.String(36), primary_key=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True), sa.Column("run_id", sa.String(36), sa.ForeignKey("solve_runs.id"), nullable=False), sa.Column("version", sa.Integer(), nullable=False, server_default="0"), sa.Column("stage", sa.String(40), nullable=False), _json("working_memory_json", "{}"), _json("verified_fact_ids_json", "[]"), _json("hypothesis_ids_json", "[]"), _json("evidence_ids_json", "[]"), sa.Column("created_by_task_id", sa.String(36), sa.ForeignKey("agent_tasks.id")), sa.Column("is_current", sa.Boolean(), nullable=False, server_default="1"),
        )
        op.create_index("ix_memory_snapshots_run_id", "memory_snapshots", ["run_id"])


def downgrade() -> None:
    for table in ("memory_snapshots", "failure_signatures", "solution_chain_nodes", "evidence_ledger", "verified_facts", "analysis_reviews", "planner_proposals", "agent_task_results", "agent_tasks", "agent_role_policies"):
        if table in _tables():
            op.drop_table(table)
    if "solve_runs" in _tables():
        for column in ("controller_revision", "solver_mode"):
            if column in _columns("solve_runs"):
                op.drop_column("solve_runs", column)
