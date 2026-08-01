"""Add model-reviewed compaction checkpoints and evidence snapshots."""

import sqlalchemy as sa

from alembic import op

revision = "0018_compaction_checkpoints"
down_revision = "0017_terminal_tool_tickets"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    tables = _tables()
    if "solve_runs" in tables:
        columns = _columns("solve_runs")
        fields = (
            ("last_compaction_effective_tool_count", sa.Integer(), "0"),
            ("compaction_generation", sa.Integer(), "0"),
            ("compaction_status", sa.String(30), "'IDLE'"),
            ("compaction_started_at", sa.DateTime(timezone=True), None),
            ("compaction_finished_at", sa.DateTime(timezone=True), None),
            ("compacted_event_count", sa.Integer(), "0"),
            ("compacted_observation_count", sa.Integer(), "0"),
            ("compacted_artifact_count", sa.Integer(), "0"),
            ("compacted_trace_count", sa.Integer(), "0"),
            ("last_compaction_snapshot_id", sa.String(36), None),
        )
        for name, kind, default in fields:
            if name not in columns:
                op.add_column("solve_runs", sa.Column(name, kind, nullable=True, server_default=default))
    if "run_events" in tables and "event_id" not in _columns("run_events"):
            # Keep this nullable for legacy rows; MySQL deployments can promote
            # it to AUTO_INCREMENT.
        op.add_column("run_events", sa.Column("event_id", sa.BigInteger(), nullable=True, autoincrement=True))
        op.create_index("ix_run_events_event_id", "run_events", ["event_id"])

    if "run_compaction_checkpoints" not in tables:
        op.create_table(
            "run_compaction_checkpoints",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("run_id", sa.String(36), sa.ForeignKey("solve_runs.id"), nullable=False),
            sa.Column("generation", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(30), nullable=False, server_default="COMPACTION_REVIEW"),
            # MySQL rejects defaults on TEXT/BLOB columns.  The ORM supplies
            # the empty string for new rows; the migration intentionally has
            # no database-level default here.
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("decision_json", sa.JSON(), nullable=False),
            sa.Column("archive_path", sa.String(1024)),
            sa.Column("archive_manifest_json", sa.JSON(), nullable=False),
            sa.Column("deleted_row_counts_json", sa.JSON(), nullable=False),
            sa.Column("error_code", sa.String(100)),
            sa.Column("finished_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("run_id", "generation", name="uq_compaction_run_generation"),
        )
        op.create_index("ix_run_compaction_checkpoints_run_id", "run_compaction_checkpoints", ["run_id"])
    if "run_evidence_snapshots" not in tables:
        op.create_table(
            "run_evidence_snapshots",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("run_id", sa.String(36), sa.ForeignKey("solve_runs.id"), nullable=False),
            sa.Column("generation", sa.Integer(), nullable=False),
            sa.Column("snapshot_json", sa.JSON(), nullable=False),
            sa.Column("sha256", sa.String(64), nullable=False, server_default=""),
            sa.Column("source_checkpoint_id", sa.String(36), sa.ForeignKey("run_compaction_checkpoints.id")),
            sa.Column("is_current", sa.Boolean(), nullable=False, server_default="1"),
        )
        op.create_index("ix_run_evidence_snapshots_run_id", "run_evidence_snapshots", ["run_id"])


def downgrade() -> None:
    tables = _tables()
    if "run_evidence_snapshots" in tables:
        op.drop_table("run_evidence_snapshots")
    if "run_compaction_checkpoints" in tables:
        op.drop_table("run_compaction_checkpoints")
    if "run_events" in tables and "event_id" in _columns("run_events"):
        op.drop_index("ix_run_events_event_id", table_name="run_events")
        op.drop_column("run_events", "event_id")
    if "solve_runs" in tables:
        for name in (
            "last_compaction_snapshot_id", "compacted_trace_count", "compacted_artifact_count",
            "compacted_observation_count", "compacted_event_count", "compaction_finished_at",
            "compaction_started_at", "compaction_status", "compaction_generation",
            "last_compaction_effective_tool_count",
        ):
            if name in _columns("solve_runs"):
                op.drop_column("solve_runs", name)
