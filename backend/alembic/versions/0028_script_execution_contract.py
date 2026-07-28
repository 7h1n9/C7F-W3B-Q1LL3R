"""Persist bounded script lifecycle and independent required-action budget."""

import sqlalchemy as sa

from alembic import op


revision = "0028_script_execution_contract"
down_revision = "0027_default_multi_agent_mode"
branch_labels = None
depends_on = None


def _add(table: str, name: str, column: sa.Column) -> None:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}
    if name not in columns:
        op.add_column(table, column)


def upgrade() -> None:
    _add("solve_runs", "reserved_required_action_calls", sa.Column("reserved_required_action_calls", sa.Integer(), nullable=False, server_default="0"))
    # MySQL rejects a JSON/TEXT server default.  Legacy rows may be NULL; the
    # ORM supplies an empty mapping for new rows.
    _add("solve_runs", "reserved_required_action_calls_by_type_json", sa.Column("reserved_required_action_calls_by_type_json", sa.JSON(), nullable=True))
    _add("solve_runs", "required_action_calls_used", sa.Column("required_action_calls_used", sa.Integer(), nullable=False, server_default="0"))
    _add("run_attempts", "runtime_build_manifest_json", sa.Column("runtime_build_manifest_json", sa.JSON(), nullable=True))
    _add("run_attempts", "tool_manifest_status", sa.Column("tool_manifest_status", sa.String(40), nullable=False, server_default="UNSET"))

    script_columns = {
        "script_path": sa.Column("script_path", sa.String(1024), nullable=True),
        "agent_task_id": sa.Column("agent_task_id", sa.String(36), nullable=True),
        "tool_call_id": sa.Column("tool_call_id", sa.String(36), nullable=True),
        "objective": sa.Column("objective", sa.Text(), nullable=True),
        "network_mode": sa.Column("network_mode", sa.String(40), nullable=True, server_default="none"),
        "allowed_hosts_json": sa.Column("allowed_hosts_json", sa.JSON(), nullable=True),
        "max_requests": sa.Column("max_requests", sa.Integer(), nullable=True, server_default="0"),
        "max_runtime_seconds": sa.Column("max_runtime_seconds", sa.Integer(), nullable=True, server_default="60"),
        "validation_error": sa.Column("validation_error", sa.Text(), nullable=True),
        "execution_error": sa.Column("execution_error", sa.Text(), nullable=True),
        "result_artifact_id": sa.Column("result_artifact_id", sa.String(36), nullable=True),
        "checkpoint_artifact_id": sa.Column("checkpoint_artifact_id", sa.String(36), nullable=True),
    }
    for name, column in script_columns.items():
        _add("scripts", name, column)
    _add("scripts", "status", sa.Column("status", sa.String(20), nullable=False, server_default="CREATED"))

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    script_columns_now = {item["name"] for item in inspector.get_columns("scripts")}
    if "script_path" in script_columns_now and "path" in script_columns_now:
        op.execute(sa.text("UPDATE scripts SET script_path = path WHERE script_path IS NULL"))
    if "allowed_hosts_json" in script_columns_now:
        # Leave NULL values compatible with old rows; the ORM supplies an
        # empty list for new records.
        pass


def downgrade() -> None:
    for table, names in {
        "scripts": ["checkpoint_artifact_id", "result_artifact_id", "execution_error", "validation_error", "max_runtime_seconds", "max_requests", "allowed_hosts_json", "network_mode", "objective", "tool_call_id", "agent_task_id", "script_path"],
        "run_attempts": ["tool_manifest_status", "runtime_build_manifest_json"],
        "solve_runs": ["required_action_calls_used", "reserved_required_action_calls_by_type_json", "reserved_required_action_calls"],
    }.items():
        columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}
        for name in names:
            if name in columns:
                op.drop_column(table, name)
