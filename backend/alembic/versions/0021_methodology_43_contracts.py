"""Add explicit turn, logical-tool, infrastructure and compaction contracts."""

import sqlalchemy as sa

from alembic import op

revision = "0021_methodology_43_contracts"
down_revision = "0020_script_provenance"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def _add(table: str, name: str, column: sa.Column) -> None:
    if name not in _columns(table):
        op.add_column(table, column)


def upgrade() -> None:
    _add("solve_runs", "infrastructure_error_streak", sa.Column("infrastructure_error_streak", sa.Integer(), nullable=False, server_default="0"))
    _add("solve_runs", "infrastructure_state", sa.Column("infrastructure_state", sa.String(40), nullable=False, server_default="HEALTHY"))
    _add("solve_runs", "infrastructure_last_error_json", sa.Column("infrastructure_last_error_json", sa.JSON(), nullable=True))
    _add("solve_runs", "active_turn_id", sa.Column("active_turn_id", sa.String(36), nullable=True))
    _add("solve_runs", "reserved_tool_calls_by_turn_json", sa.Column("reserved_tool_calls_by_turn_json", sa.JSON(), nullable=True))
    _add("agent_turns", "turn_started_at", sa.Column("turn_started_at", sa.DateTime(timezone=True), nullable=True))
    _add("agent_turns", "turn_finished_at", sa.Column("turn_finished_at", sa.DateTime(timezone=True), nullable=True))
    for name, column in (
        ("counts_toward_budget", sa.Column("counts_toward_budget", sa.Boolean(), nullable=False, server_default="1")),
        ("logical_kind", sa.Column("logical_kind", sa.String(40), nullable=False, server_default="TOOL")),
        ("provider_tool_name", sa.Column("provider_tool_name", sa.String(120), nullable=True)),
        ("effective_tool_name", sa.Column("effective_tool_name", sa.String(120), nullable=True)),
        ("turn_id", sa.Column("turn_id", sa.String(36), nullable=True)),
    ):
        _add("tool_calls", name, column)
        _add("logical_tool_calls", name, column.copy())
    _add("logical_tool_calls", "turn_started_at", sa.Column("turn_started_at", sa.DateTime(timezone=True), nullable=True))
    for table in ("solve_runs", "agent_turns", "tool_calls", "logical_tool_calls"):
        indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table)}
        if table == "solve_runs" and "ix_solve_runs_active_turn_id" not in indexes:
            op.create_index("ix_solve_runs_active_turn_id", table, ["active_turn_id"])
        if table == "agent_turns":
            continue
        if table == "tool_calls" and "ix_tool_calls_turn_id" not in indexes:
            op.create_index("ix_tool_calls_turn_id", table, ["turn_id"])
        if table == "logical_tool_calls" and "ix_logical_tool_calls_turn_id" not in indexes:
            op.create_index("ix_logical_tool_calls_turn_id", table, ["turn_id"])
    if "compaction_leases" not in set(sa.inspect(op.get_bind()).get_table_names()):
        op.create_table(
            "compaction_leases",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("run_id", sa.String(36), sa.ForeignKey("solve_runs.id"), nullable=False),
            sa.Column("worker_id", sa.String(120), nullable=False),
            sa.Column("lease_token", sa.String(120), nullable=False, unique=True),
            sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
            sa.UniqueConstraint("run_id", name="uq_compaction_lease_run"),
        )
        op.create_index("ix_compaction_leases_run_id", "compaction_leases", ["run_id"])


def downgrade() -> None:
    if "compaction_leases" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("compaction_leases")
    for table, names in {
        "logical_tool_calls": ["turn_started_at", "turn_id", "effective_tool_name", "provider_tool_name", "logical_kind", "counts_toward_budget"],
        "tool_calls": ["turn_id", "effective_tool_name", "provider_tool_name", "logical_kind", "counts_toward_budget"],
        "agent_turns": ["turn_finished_at", "turn_started_at"],
            "solve_runs": ["reserved_tool_calls_by_turn_json", "active_turn_id", "infrastructure_last_error_json", "infrastructure_state", "infrastructure_error_streak"],
    }.items():
        columns = _columns(table)
        for name in names:
            if name in columns:
                op.drop_column(table, name)
