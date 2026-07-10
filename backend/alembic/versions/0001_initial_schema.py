"""initial CTF platform schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-07-10
"""
import sqlalchemy as sa

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("challenges", sa.Column("id", sa.String(36), primary_key=True), sa.Column("name", sa.String(200), nullable=False), sa.Column("description", sa.Text(), nullable=False), sa.Column("target_url", sa.String(2048), nullable=False), sa.Column("allowed_hosts", sa.JSON(), nullable=False), sa.Column("flag_pattern", sa.String(500), nullable=False), sa.Column("source_path", sa.String(1024)), sa.Column("status", sa.String(40), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("model_configs", sa.Column("id", sa.String(36), primary_key=True), sa.Column("name", sa.String(200), nullable=False, unique=True), sa.Column("provider_type", sa.String(40), nullable=False), sa.Column("base_url", sa.String(2048)), sa.Column("model_name", sa.String(255)), sa.Column("encrypted_api_key", sa.String(2048)), sa.Column("enabled", sa.Boolean(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("solve_runs", sa.Column("id", sa.String(36), primary_key=True), sa.Column("challenge_id", sa.String(36), sa.ForeignKey("challenges.id"), nullable=False), sa.Column("engine_type", sa.String(40), nullable=False), sa.Column("model_config_id", sa.String(36), sa.ForeignKey("model_configs.id")), sa.Column("status", sa.String(40), nullable=False), sa.Column("current_phase", sa.String(80), nullable=False), sa.Column("workspace_path", sa.String(1024), nullable=False), sa.Column("codex_thread_id", sa.String(255)), sa.Column("started_at", sa.DateTime(timezone=True)), sa.Column("finished_at", sa.DateTime(timezone=True)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("run_events", sa.Column("id", sa.String(36), primary_key=True), sa.Column("run_id", sa.String(36), sa.ForeignKey("solve_runs.id"), nullable=False), sa.Column("sequence", sa.Integer(), nullable=False), sa.Column("event_type", sa.String(100), nullable=False), sa.Column("payload_json", sa.JSON(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("run_id", "sequence", name="uq_run_event_sequence"))
    for table in ("tool_calls", "artifacts", "observations", "hypotheses", "flag_candidates"):
        if table == "tool_calls": op.create_table(table, sa.Column("id", sa.String(36), primary_key=True), sa.Column("run_id", sa.String(36), sa.ForeignKey("solve_runs.id"), nullable=False), sa.Column("tool_name", sa.String(100), nullable=False), sa.Column("arguments_json", sa.JSON(), nullable=False), sa.Column("status", sa.String(40), nullable=False), sa.Column("runner_job_id", sa.String(255)), sa.Column("started_at", sa.DateTime(timezone=True)), sa.Column("finished_at", sa.DateTime(timezone=True)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
        elif table == "artifacts": op.create_table(table, sa.Column("id", sa.String(36), primary_key=True), sa.Column("run_id", sa.String(36), sa.ForeignKey("solve_runs.id"), nullable=False), sa.Column("tool_call_id", sa.String(36), sa.ForeignKey("tool_calls.id")), sa.Column("artifact_type", sa.String(80), nullable=False), sa.Column("file_path", sa.String(1024), nullable=False), sa.Column("mime_type", sa.String(255), nullable=False), sa.Column("size", sa.Integer(), nullable=False), sa.Column("sha256", sa.String(64), nullable=False), sa.Column("summary", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
        elif table == "observations": op.create_table(table, sa.Column("id", sa.String(36), primary_key=True), sa.Column("run_id", sa.String(36), sa.ForeignKey("solve_runs.id"), nullable=False), sa.Column("tool_call_id", sa.String(36), sa.ForeignKey("tool_calls.id")), sa.Column("artifact_id", sa.String(36), sa.ForeignKey("artifacts.id")), sa.Column("observation_type", sa.String(80), nullable=False), sa.Column("summary", sa.Text(), nullable=False), sa.Column("facts_json", sa.JSON(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
        elif table == "hypotheses": op.create_table(table, sa.Column("id", sa.String(36), primary_key=True), sa.Column("run_id", sa.String(36), sa.ForeignKey("solve_runs.id"), nullable=False), sa.Column("category", sa.String(80), nullable=False), sa.Column("title", sa.String(255), nullable=False), sa.Column("description", sa.Text(), nullable=False), sa.Column("confidence", sa.Integer(), nullable=False), sa.Column("priority", sa.Integer(), nullable=False), sa.Column("status", sa.String(40), nullable=False), sa.Column("evidence_json", sa.JSON(), nullable=False), sa.Column("attempt_count", sa.Integer(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
        else: op.create_table(table, sa.Column("id", sa.String(36), primary_key=True), sa.Column("run_id", sa.String(36), sa.ForeignKey("solve_runs.id"), nullable=False), sa.Column("candidate", sa.String(1000), nullable=False), sa.Column("source_artifact_id", sa.String(36), sa.ForeignKey("artifacts.id")), sa.Column("pattern_matched", sa.Boolean(), nullable=False), sa.Column("verified", sa.Boolean(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))


def downgrade() -> None:
    for table in ("flag_candidates", "hypotheses", "observations", "artifacts", "tool_calls", "run_events", "solve_runs", "model_configs", "challenges"):
        op.drop_table(table)
