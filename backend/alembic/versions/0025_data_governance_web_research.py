"""Add temporary-data governance, provenance, batch summaries, and web research."""

import sqlalchemy as sa

from alembic import op

revision = "0025_data_governance_web_research"
down_revision = "0024_multi_agent_core"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def _json(name: str, default: str = "{}") -> sa.Column:
    # MySQL 8 rejects string defaults on JSON columns; ORM defaults provide
    # empty containers for newly inserted rows.
    return sa.Column(name, sa.JSON(), nullable=False)


def _add(table: str, name: str, kind: sa.types.TypeEngine, default: str | None = None) -> None:
    if name not in _columns(table):
        op.add_column(table, sa.Column(name, kind, nullable=False, server_default=default) if default is not None else sa.Column(name, kind, nullable=True))


def upgrade() -> None:
    tables = _tables()
    if "solve_runs" in tables:
        for name, kind, default in (
            ("terminal_cleanup_completed", sa.Boolean(), "0"),
            ("terminal_cleanup_manifest_id", sa.String(36), None),
            ("terminal_cleanup_at", sa.DateTime(timezone=True), None),
            ("terminal_evidence_snapshot_id", sa.String(36), None),
            ("cleanup_generation", sa.Integer(), "0"),
        ):
            _add("solve_runs", name, kind, default)
    if "artifacts" in tables:
        for name, kind, default in (
            ("retention_class", sa.String(30), "PROTECTED"),
            ("temporary", sa.Boolean(), "0"),
            ("terminal_referenced", sa.Boolean(), "0"),
            ("promoted_at", sa.DateTime(timezone=True), None),
        ):
            _add("artifacts", name, kind, default)
    if "flag_candidates" in tables:
        for name, kind, default in (
            ("first_seen_source_type", sa.String(30), None),
            ("first_seen_source_id", sa.String(36), None),
            ("first_seen_at", sa.DateTime(timezone=True), None),
            ("source_tool_call_id", sa.String(36), None),
            ("source_agent_task_id", sa.String(36), None),
            ("source_assistance_level", sa.String(30), "AUTONOMOUS"),
        ):
            _add("flag_candidates", name, kind, default)
        if "ix_flag_candidates_source_agent_task_id" not in {idx["name"] for idx in sa.inspect(op.get_bind()).get_indexes("flag_candidates")}:
            op.create_index("ix_flag_candidates_source_agent_task_id", "flag_candidates", ["source_agent_task_id"])
    if "agent_tasks" in tables:
        _add("agent_tasks", "runtime_path", sa.String(1024), None)

    if "flag_provenance" not in tables:
        op.create_table(
            "flag_provenance",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("run_id", sa.String(36), sa.ForeignKey("solve_runs.id"), nullable=False),
            sa.Column("candidate_id", sa.String(36), sa.ForeignKey("flag_candidates.id"), nullable=False),
            sa.Column("first_seen_source_type", sa.String(30), nullable=False),
            sa.Column("first_seen_source_id", sa.String(36)),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("source_artifact_id", sa.String(36), sa.ForeignKey("artifacts.id")),
            sa.Column("source_tool_call_id", sa.String(36), sa.ForeignKey("tool_calls.id")),
            sa.Column("source_agent_task_id", sa.String(36)),
            sa.Column("source_assistance_level", sa.String(30), nullable=False, server_default="AUTONOMOUS"),
            sa.Column("verification_source_type", sa.String(30)),
            sa.Column("verification_source_id", sa.String(36)),
            sa.Column("source_is_autonomous", sa.Boolean(), nullable=False, server_default="1"),
            sa.UniqueConstraint("candidate_id", name="uq_flag_provenance_candidate"),
        )
        op.create_index("ix_flag_provenance_run_id", "flag_provenance", ["run_id"])

    if "cleanup_manifests" not in tables:
        op.create_table(
            "cleanup_manifests",
            sa.Column("id", sa.String(36), primary_key=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("run_id", sa.String(36), sa.ForeignKey("solve_runs.id"), nullable=False), sa.Column("agent_task_id", sa.String(36)),
            sa.Column("cleanup_kind", sa.String(40), nullable=False), sa.Column("status", sa.String(30), nullable=False, server_default="PLANNED"),
            sa.Column("idempotency_key", sa.String(255), nullable=False), _json("manifest_json", "{}"), sa.Column("sha256", sa.String(64), nullable=False, server_default=""),
            sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"), sa.Column("archive_path", sa.String(1024)), sa.Column("archive_sha256", sa.String(64)),
            sa.Column("retention_deadline", sa.DateTime(timezone=True)), sa.Column("completed_at", sa.DateTime(timezone=True)), _json("deleted_paths_json", "[]"), _json("preserved_paths_json", "[]"),
            sa.Column("debug_mode", sa.Boolean(), nullable=False, server_default="0"), sa.UniqueConstraint("idempotency_key", name="uq_cleanup_manifest_idempotency"),
        )
        op.create_index("ix_cleanup_manifests_run_id", "cleanup_manifests", ["run_id"])
        op.create_index("ix_cleanup_manifests_agent_task_id", "cleanup_manifests", ["agent_task_id"])

    if "tool_batch_summaries" not in tables:
        op.create_table(
            "tool_batch_summaries",
            sa.Column("id", sa.String(36), primary_key=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("run_id", sa.String(36), sa.ForeignKey("solve_runs.id"), nullable=False), sa.Column("agent_task_id", sa.String(36)), sa.Column("logical_tool_call_id", sa.String(120), nullable=False), sa.Column("tool_call_id", sa.String(36), sa.ForeignKey("tool_calls.id")), sa.Column("tool_name", sa.String(100), nullable=False), sa.Column("subrequest_count", sa.Integer(), nullable=False, server_default="0"), sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"), sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"), sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"), sa.Column("bytes_received", sa.BigInteger(), nullable=False, server_default="0"), sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"), sa.Column("result_artifact_id", sa.String(36), sa.ForeignKey("artifacts.id")), sa.Column("result_artifact_path", sa.String(1024)), sa.Column("status", sa.String(30), nullable=False, server_default="COMPLETED"), sa.UniqueConstraint("run_id", "logical_tool_call_id", name="uq_tool_batch_run_logical"),
        )
        op.create_index("ix_tool_batch_summaries_run_id", "tool_batch_summaries", ["run_id"])

    if "tool_request_fingerprints" not in tables:
        op.create_table(
            "tool_request_fingerprints",
            sa.Column("id", sa.String(36), primary_key=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("run_id", sa.String(36), sa.ForeignKey("solve_runs.id"), nullable=False), sa.Column("fingerprint", sa.String(64), nullable=False), sa.Column("tool_name", sa.String(100), nullable=False), _json("normalized_arguments_json", "{}"), sa.Column("stage", sa.String(80), nullable=False, server_default=""), sa.Column("evidence_version", sa.Integer(), nullable=False, server_default="0"), sa.Column("logical_tool_call_id", sa.String(120)), sa.Column("status", sa.String(30), nullable=False, server_default="SCHEDULED"), sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("run_id", "fingerprint", name="uq_tool_request_run_fingerprint"),
        )
        op.create_index("ix_tool_request_fingerprints_run_id", "tool_request_fingerprints", ["run_id"])

    if "web_research_records" not in tables:
        op.create_table(
            "web_research_records",
            sa.Column("id", sa.String(36), primary_key=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("run_id", sa.String(36), sa.ForeignKey("solve_runs.id"), nullable=False), sa.Column("agent_task_id", sa.String(36)), sa.Column("query", sa.Text(), nullable=False), sa.Column("query_type", sa.String(40), nullable=False), sa.Column("requested_by", sa.String(30), nullable=False), sa.Column("risk_level", sa.String(20), nullable=False), sa.Column("answer_leak_risk", sa.String(20), nullable=False), sa.Column("status", sa.String(30), nullable=False, server_default="EPHEMERAL"), _json("source_urls_json", "[]"), sa.Column("summary", sa.Text(), nullable=False), _json("used_in_fact_ids_json", "[]"), sa.Column("runtime_path", sa.String(1024)), sa.Column("expires_at", sa.DateTime(timezone=True)), sa.Column("promoted_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_web_research_records_run_id", "web_research_records", ["run_id"])
        op.create_index("ix_web_research_records_agent_task_id", "web_research_records", ["agent_task_id"])


def downgrade() -> None:
    tables = _tables()
    for table in ("web_research_records", "tool_request_fingerprints", "tool_batch_summaries", "cleanup_manifests", "flag_provenance"):
        if table in tables:
            op.drop_table(table)
    if "agent_tasks" in _tables() and "runtime_path" in _columns("agent_tasks"):
        op.drop_column("agent_tasks", "runtime_path")
    if "flag_candidates" in _tables():
        if "ix_flag_candidates_source_agent_task_id" in {idx["name"] for idx in sa.inspect(op.get_bind()).get_indexes("flag_candidates")}: op.drop_index("ix_flag_candidates_source_agent_task_id", table_name="flag_candidates")
        for name in ("source_assistance_level", "source_agent_task_id", "source_tool_call_id", "first_seen_at", "first_seen_source_id", "first_seen_source_type"):
            if name in _columns("flag_candidates"): op.drop_column("flag_candidates", name)
    if "artifacts" in _tables():
        for name in ("promoted_at", "terminal_referenced", "temporary", "retention_class"):
            if name in _columns("artifacts"): op.drop_column("artifacts", name)
    if "solve_runs" in _tables():
        for name in ("cleanup_generation", "terminal_evidence_snapshot_id", "terminal_cleanup_at", "terminal_cleanup_manifest_id", "terminal_cleanup_completed"):
            if name in _columns("solve_runs"): op.drop_column("solve_runs", name)
