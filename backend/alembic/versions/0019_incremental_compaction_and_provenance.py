"""Add incremental compaction watermarks, reservations and provenance."""

import sqlalchemy as sa
from alembic import op


revision = "0019_incremental_compaction_and_provenance"
down_revision = "0018_compaction_checkpoints"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    run_columns = _columns("solve_runs")
    fields = (
        ("reserved_tool_calls", sa.Integer(), "0"),
        ("assistance_level", sa.String(30), "'AUTONOMOUS'"),
        ("assistance_sources_json", sa.JSON(), None),
        ("last_compacted_event_id", sa.BigInteger(), "0"),
        ("last_compacted_tool_created_at", sa.DateTime(timezone=True), None),
        ("last_compacted_observation_created_at", sa.DateTime(timezone=True), None),
        ("last_compacted_artifact_created_at", sa.DateTime(timezone=True), None),
        ("last_compacted_trace_created_at", sa.DateTime(timezone=True), None),
    )
    for name, kind, default in fields:
        if name not in run_columns:
            kwargs = {"nullable": False, "server_default": default} if default is not None else {"nullable": True}
            op.add_column("solve_runs", sa.Column(name, kind, **kwargs))
    event_columns = _columns("run_events")
    if "payload_size" not in event_columns:
        op.add_column("run_events", sa.Column("payload_size", sa.Integer(), nullable=False, server_default="0"))
    if "payload_digest" not in event_columns:
        op.add_column("run_events", sa.Column("payload_digest", sa.String(64), nullable=False, server_default=""))
    indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("run_events")}
    if "ix_run_events_payload_digest" not in indexes:
        op.create_index("ix_run_events_payload_digest", "run_events", ["run_id", "payload_digest"])
    constraints = {item["name"] for item in sa.inspect(op.get_bind()).get_unique_constraints("tool_execution_traces")}
    if "uq_tool_trace_identity" not in constraints:
        connection = op.get_bind()
        traces = sa.Table("tool_execution_traces", sa.MetaData(), autoload_with=connection)
        identity = [traces.c.logical_tool_call_id, traces.c.execution_layer, traces.c.event_type, traces.c.external_id, traces.c.payload_digest]
        # Old rows may have NULL external ids, which are not equal under a
        # MySQL UNIQUE constraint. Normalize them and retain the newest row
        # before installing the idempotency constraint.
        connection.execute(traces.update().where(traces.c.external_id.is_(None)).values(external_id=""))
        duplicates = connection.execute(
            sa.select(*identity, sa.func.count().label("row_count"))
            .group_by(*identity)
            .having(sa.func.count() > 1)
        ).mappings().all()
        for duplicate in duplicates:
            predicate = sa.and_(*[column == duplicate[column.name] for column in identity])
            ids = connection.execute(sa.select(traces.c.id).where(predicate).order_by(traces.c.created_at.desc())).scalars().all()
            if len(ids) > 1:
                connection.execute(traces.delete().where(traces.c.id.in_(ids[1:])))
        if connection.dialect.name == "sqlite":
            with op.batch_alter_table("tool_execution_traces") as batch:
                batch.create_unique_constraint(
                    "uq_tool_trace_identity",
                    ["logical_tool_call_id", "execution_layer", "event_type", "external_id", "payload_digest"],
                )
        else:
            op.create_unique_constraint(
                "uq_tool_trace_identity",
                "tool_execution_traces",
                ["logical_tool_call_id", "execution_layer", "event_type", "external_id", "payload_digest"],
            )


def downgrade() -> None:
    bind = op.get_bind()
    constraints = {item["name"] for item in sa.inspect(bind).get_unique_constraints("tool_execution_traces")}
    if "uq_tool_trace_identity" in constraints:
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table("tool_execution_traces") as batch:
                batch.drop_constraint("uq_tool_trace_identity")
        else:
            op.drop_constraint("uq_tool_trace_identity", "tool_execution_traces", type_="unique")
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("run_events")}
    if "ix_run_events_payload_digest" in indexes:
        op.drop_index("ix_run_events_payload_digest", table_name="run_events")
    for table, names in {
        "run_events": ["payload_digest", "payload_size"],
        "solve_runs": [
            "last_compacted_trace_created_at", "last_compacted_artifact_created_at",
            "last_compacted_observation_created_at", "last_compacted_tool_created_at",
            "last_compacted_event_id", "assistance_sources_json", "assistance_level", "reserved_tool_calls",
        ],
    }.items():
        columns = _columns(table)
        for name in names:
            if name in columns:
                op.drop_column(table, name)
