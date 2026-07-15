"""Add provider capability profiles and durable AgentTurn telemetry.

Revision ID: 0008_provider_agent_turns
Revises: 0007_run_skill_recommendations
"""

import sqlalchemy as sa

from alembic import op

revision = "0008_provider_agent_turns"
down_revision = "0007_run_skill_recommendations"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    existing = _columns("model_configs")
    columns = [
        sa.Column("action_protocol", sa.String(30), nullable=True),
        sa.Column("structured_output_mode", sa.String(30), nullable=True),
        sa.Column("supports_json_schema", sa.Boolean(), nullable=True),
        sa.Column("supports_json_object", sa.Boolean(), nullable=True),
        sa.Column("supports_native_tool_call", sa.Boolean(), nullable=True),
        sa.Column("request_timeout_seconds", sa.Integer(), nullable=True),
        sa.Column("max_output_tokens", sa.Integer(), nullable=True),
        sa.Column("temperature", sa.Float(), nullable=True),
        sa.Column("max_retries", sa.Integer(), nullable=True),
        sa.Column("retry_base_seconds", sa.Float(), nullable=True),
        sa.Column("rate_limit_cooldown_seconds", sa.Integer(), nullable=True),
        sa.Column("requests_per_minute", sa.Integer(), nullable=True),
        sa.Column("max_concurrency", sa.Integer(), nullable=True),
        sa.Column("context_token_limit", sa.Integer(), nullable=True),
        sa.Column("last_test_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_ok", sa.Boolean(), nullable=True),
        sa.Column("capabilities_json", sa.JSON(), nullable=True),
    ]
    for column in columns:
        if column.name not in existing:
            op.add_column("model_configs", column)
    op.execute(sa.text("UPDATE model_configs SET action_protocol='json_schema' WHERE action_protocol IS NULL"))
    op.execute(sa.text("UPDATE model_configs SET structured_output_mode='json_schema' WHERE structured_output_mode IS NULL"))
    op.execute(sa.text("UPDATE model_configs SET request_timeout_seconds=30 WHERE request_timeout_seconds IS NULL"))
    op.execute(sa.text("UPDATE model_configs SET max_output_tokens=2048 WHERE max_output_tokens IS NULL"))
    op.execute(sa.text("UPDATE model_configs SET temperature=0 WHERE temperature IS NULL"))
    op.execute(sa.text("UPDATE model_configs SET max_retries=2 WHERE max_retries IS NULL"))
    op.execute(sa.text("UPDATE model_configs SET retry_base_seconds=1 WHERE retry_base_seconds IS NULL"))
    op.execute(sa.text("UPDATE model_configs SET rate_limit_cooldown_seconds=60 WHERE rate_limit_cooldown_seconds IS NULL"))
    op.execute(sa.text("UPDATE model_configs SET requests_per_minute=60 WHERE requests_per_minute IS NULL"))
    op.execute(sa.text("UPDATE model_configs SET max_concurrency=2 WHERE max_concurrency IS NULL"))
    op.execute(sa.text("UPDATE model_configs SET context_token_limit=128000 WHERE context_token_limit IS NULL"))
    op.execute(sa.text("UPDATE model_configs SET capabilities_json='{}' WHERE capabilities_json IS NULL"))

    if "agent_turns" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "agent_turns",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("run_id", sa.String(36), sa.ForeignKey("solve_runs.id"), nullable=False, index=True),
            sa.Column("step_number", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("model_config_id", sa.String(36), sa.ForeignKey("model_configs.id")),
            sa.Column("action_protocol", sa.String(30), nullable=False, server_default="json_schema"),
            sa.Column("prompt_hash", sa.String(64), nullable=False, server_default=""),
            sa.Column("context_size_chars", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("provider_request_id", sa.String(255)),
            sa.Column("latency_ms", sa.Integer()),
            sa.Column("input_tokens", sa.Integer()),
            sa.Column("output_tokens", sa.Integer()),
            sa.Column("parse_attempts", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("parse_error_code", sa.String(100)),
            sa.Column("response_excerpt_redacted", sa.Text()),
            sa.Column("action_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    if "agent_turns" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("agent_turns")
    for name in {
        "action_protocol", "structured_output_mode", "supports_json_schema", "supports_json_object",
        "supports_native_tool_call", "request_timeout_seconds", "max_output_tokens", "temperature",
        "max_retries", "retry_base_seconds", "rate_limit_cooldown_seconds", "requests_per_minute",
        "max_concurrency", "context_token_limit", "last_test_at", "last_test_ok", "capabilities_json",
    }:
        if name in _columns("model_configs"):
            op.drop_column("model_configs", name)
