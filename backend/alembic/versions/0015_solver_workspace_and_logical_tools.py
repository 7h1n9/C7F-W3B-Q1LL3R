"""Add logical tool identity, solver planning state, and read indexes."""
import sqlalchemy as sa

from alembic import op

revision = "0015_solver_workspace_and_logical_tools"
down_revision = "0014_solver_recovery_controls"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {item["name"] for item in inspector.get_columns(table)} if table in inspector.get_table_names() else set()


def upgrade() -> None:
    # Older migrations created a short alembic_version column. The new
    # revision identifier is longer, so widen it before Alembic writes the
    # revision marker. Keep the SQLite path untouched for local migration
    # validation.
    if op.get_bind().dialect.name == "mysql":
        op.execute("ALTER TABLE alembic_version MODIFY COLUMN version_num VARCHAR(128) NOT NULL")

    tool_columns = _columns("tool_calls")
    for name, kind in (("logical_tool_call_id", sa.String(120)), ("parent_tool_call_id", sa.String(120)), ("execution_layer", sa.String(40))):
        if name not in tool_columns:
            op.add_column("tool_calls", sa.Column(name, kind, nullable=(name != "execution_layer"), server_default="gateway" if name == "execution_layer" else None))
    state_columns = _columns("solver_states")
    definitions = {
        "run_plan_json": (sa.JSON(), "{}"),
        "capability_ledger_json": (sa.JSON(), "{}"),
        "read_files_json": (sa.JSON(), "[]"),
        "read_ranges_json": (sa.JSON(), "[]"),
        "content_hashes_json": (sa.JSON(), "{}"),
        "last_decision_card_json": (sa.JSON(), "{}"),
        "last_experiment_json": (sa.JSON(), "{}"),
    }
    for name, (kind, empty_value) in definitions.items():
        if name not in state_columns:
            # MySQL rejects DEFAULT values on JSON columns. Add the column
            # without a database default, then backfill existing rows. ORM
            # defaults cover new rows after this migration.
            op.add_column("solver_states", sa.Column(name, kind, nullable=True))
        op.execute(
            sa.text(f"UPDATE solver_states SET {name} = :empty_value WHERE {name} IS NULL").bindparams(
                empty_value=empty_value
            )
        )


def downgrade() -> None:
    for name in ("last_experiment_json", "last_decision_card_json", "content_hashes_json", "read_ranges_json", "read_files_json", "capability_ledger_json", "run_plan_json"):
        if name in _columns("solver_states"):
            op.drop_column("solver_states", name)
    for name in ("execution_layer", "parent_tool_call_id", "logical_tool_call_id"):
        if name in _columns("tool_calls"):
            op.drop_column("tool_calls", name)
