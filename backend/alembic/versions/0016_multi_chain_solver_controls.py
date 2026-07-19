"""Add durable multi-chain counters and finish/recovery state."""

import sqlalchemy as sa

from alembic import op

revision = "0016_multi_chain_solver_controls"
down_revision = "0015_solver_workspace_and_logical_tools"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return (
        {item["name"] for item in inspector.get_columns(table)}
        if table in inspector.get_table_names()
        else set()
    )


def upgrade() -> None:
    run_columns = _columns("solve_runs")
    for name, default in (
        ("max_total_runtime_seconds", "3600"),
        ("run_total_agent_steps", "0"),
        ("run_total_logical_tool_calls", "0"),
        ("attempt_agent_steps", "0"),
        ("attempt_logical_tool_calls", "0"),
        ("checkpoint_segment_steps", "0"),
        ("current_attempt_number", "0"),
    ):
        if name not in run_columns:
            op.add_column(
                "solve_runs",
                sa.Column(name, sa.Integer(), nullable=False, server_default=default),
            )

    attempt_columns = _columns("run_attempts")
    for name in ("attempt_agent_steps", "attempt_logical_tool_calls"):
        if name not in attempt_columns:
            op.add_column(
                "run_attempts",
                sa.Column(name, sa.Integer(), nullable=False, server_default="0"),
            )

    state_columns = _columns("solver_states")
    for name, kind in (
        ("attack_chain_plan_json", sa.JSON()),
        ("experiment_dimensions_json", sa.JSON()),
        ("last_result_classification", sa.String(40)),
        ("finish_rejection_count", sa.Integer()),
        ("force_plan_action", sa.Integer()),
    ):
        if name not in state_columns:
            # MySQL does not permit JSON server defaults. Add JSON state as
            # nullable and backfill it below; ORM defaults cover new rows.
            nullable = name == "last_result_classification" or isinstance(kind, sa.JSON)
            default = None if nullable else "0"
            op.add_column(
                "solver_states",
                sa.Column(name, kind, nullable=nullable, server_default=default),
            )

    op.execute(
        "UPDATE run_attempts SET attempt_agent_steps = agent_steps, "
        "attempt_logical_tool_calls = tool_calls"
    )
    op.execute(
        "UPDATE solve_runs SET "
        "run_total_agent_steps = CASE WHEN agent_step_count > "
        "COALESCE((SELECT SUM(ra.agent_steps) FROM run_attempts ra WHERE ra.run_id = solve_runs.id), 0) "
        "THEN agent_step_count ELSE COALESCE((SELECT SUM(ra.agent_steps) FROM run_attempts ra WHERE ra.run_id = solve_runs.id), 0) END, "
        "run_total_logical_tool_calls = CASE WHEN tool_call_count > "
        "COALESCE((SELECT SUM(ra.tool_calls) FROM run_attempts ra WHERE ra.run_id = solve_runs.id), 0) "
        "THEN tool_call_count ELSE COALESCE((SELECT SUM(ra.tool_calls) FROM run_attempts ra WHERE ra.run_id = solve_runs.id), 0) END"
    )
    op.execute(
        "UPDATE solver_states SET attack_chain_plan_json = '{}' "
        "WHERE attack_chain_plan_json IS NULL"
    )
    op.execute(
        "UPDATE solver_states SET experiment_dimensions_json = '[]' "
        "WHERE experiment_dimensions_json IS NULL"
    )


def downgrade() -> None:
    for name in (
        "force_plan_action",
        "finish_rejection_count",
        "last_result_classification",
        "experiment_dimensions_json",
        "attack_chain_plan_json",
    ):
        if name in _columns("solver_states"):
            op.drop_column("solver_states", name)
    for name in ("attempt_logical_tool_calls", "attempt_agent_steps"):
        if name in _columns("run_attempts"):
            op.drop_column("run_attempts", name)
    for name in (
        "current_attempt_number",
        "checkpoint_segment_steps",
        "attempt_logical_tool_calls",
        "attempt_agent_steps",
        "run_total_logical_tool_calls",
        "run_total_agent_steps",
        "max_total_runtime_seconds",
    ):
        if name in _columns("solve_runs"):
            op.drop_column("solve_runs", name)
