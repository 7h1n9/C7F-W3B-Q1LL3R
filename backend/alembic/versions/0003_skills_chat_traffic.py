"""add skills, challenge chat, attachments, and traffic challenge support

Revision ID: 0003_skills_chat_traffic
Revises: 0002_agent_loop
"""
import sqlalchemy as sa

from alembic import op

revision = "0003_skills_chat_traffic"
down_revision = "0002_agent_loop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skills",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("name", sa.String(120), nullable=False, unique=True),
        sa.Column("display_name", sa.String(200), nullable=False), sa.Column("description", sa.Text(), nullable=False),
        sa.Column("source_type", sa.String(20), nullable=False), sa.Column("challenge_types", sa.JSON(), nullable=False),
        sa.Column("content_markdown", sa.Text(), nullable=False), sa.Column("allowed_tools", sa.JSON(), nullable=False),
        sa.Column("risk_level", sa.String(20), nullable=False), sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False), sa.Column("builtin_path", sa.String(512), unique=True),
        sa.Column("checksum", sa.String(64), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "challenge_attachments",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("challenge_id", sa.String(36), sa.ForeignKey("challenges.id"), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False), sa.Column("original_name", sa.String(512), nullable=False),
        sa.Column("stored_name", sa.String(128), nullable=False, unique=True), sa.Column("relative_path", sa.String(512), nullable=False, unique=True),
        sa.Column("mime_type", sa.String(255), nullable=False), sa.Column("size", sa.Integer(), nullable=False), sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_challenge_attachments_challenge_id", "challenge_attachments", ["challenge_id"])
    with op.batch_alter_table("challenges") as batch:
        batch.alter_column("target_url", existing_type=sa.String(2048), nullable=True)
        batch.add_column(sa.Column("challenge_type", sa.String(40), nullable=False, server_default="WEB_TARGET"))
        batch.add_column(
            sa.Column(
                "primary_attachment_id",
                sa.String(36),
                sa.ForeignKey("challenge_attachments.id", name="fk_challenges_primary_attachment"),
            )
        )
        batch.add_column(sa.Column("metadata_json", sa.JSON(), nullable=True))
    op.execute("UPDATE challenges SET metadata_json = JSON_OBJECT() WHERE metadata_json IS NULL")
    with op.batch_alter_table("challenges") as batch:
        batch.alter_column("metadata_json", existing_type=sa.JSON(), nullable=False)
    op.create_table(
        "model_skill_bindings",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("model_config_id", sa.String(36), sa.ForeignKey("model_configs.id"), nullable=False),
        sa.Column("skill_id", sa.String(36), sa.ForeignKey("skills.id"), nullable=False), sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False), sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("model_config_id", "skill_id", name="uq_model_skill_binding"),
    )
    op.create_table("challenge_skill_bindings", sa.Column("challenge_id", sa.String(36), sa.ForeignKey("challenges.id"), primary_key=True), sa.Column("skill_id", sa.String(36), sa.ForeignKey("skills.id"), primary_key=True), sa.Column("priority", sa.Integer(), nullable=False))
    op.create_table(
        "run_skill_snapshots",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("run_id", sa.String(36), sa.ForeignKey("solve_runs.id"), nullable=False),
        sa.Column("skill_id", sa.String(36), sa.ForeignKey("skills.id"), nullable=False), sa.Column("skill_name", sa.String(120), nullable=False),
        sa.Column("skill_version", sa.Integer(), nullable=False), sa.Column("content_snapshot", sa.Text(), nullable=False),
        sa.Column("allowed_tools_snapshot", sa.JSON(), nullable=False), sa.Column("config_snapshot", sa.JSON(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_run_skill_snapshots_run_id", "run_skill_snapshots", ["run_id"])
    op.create_table(
        "challenge_conversations",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("challenge_id", sa.String(36), sa.ForeignKey("challenges.id"), nullable=False),
        sa.Column("model_config_id", sa.String(36), sa.ForeignKey("model_configs.id")), sa.Column("title", sa.String(200), nullable=False),
        sa.Column("status", sa.String(40), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_challenge_conversations_challenge_id", "challenge_conversations", ["challenge_id"])
    op.create_table("challenge_conversation_skills", sa.Column("conversation_id", sa.String(36), sa.ForeignKey("challenge_conversations.id"), primary_key=True), sa.Column("skill_id", sa.String(36), sa.ForeignKey("skills.id"), primary_key=True), sa.Column("priority", sa.Integer(), nullable=False))
    op.create_table(
        "challenge_messages",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("conversation_id", sa.String(36), sa.ForeignKey("challenge_conversations.id"), nullable=False),
        sa.Column("role", sa.String(20), nullable=False), sa.Column("content", sa.Text(), nullable=False), sa.Column("status", sa.String(40), nullable=False),
        sa.Column("usage_json", sa.JSON(), nullable=False), sa.Column("error_code", sa.String(100)), sa.Column("error_message", sa.Text()), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_challenge_messages_conversation_id", "challenge_messages", ["conversation_id"])
    with op.batch_alter_table("solve_runs") as batch:
        batch.add_column(sa.Column("conversation_summary", sa.Text()))


def downgrade() -> None:
    with op.batch_alter_table("solve_runs") as batch:
        batch.drop_column("conversation_summary")
    for index, table in (("ix_challenge_messages_conversation_id", "challenge_messages"), ("ix_challenge_conversations_challenge_id", "challenge_conversations"), ("ix_run_skill_snapshots_run_id", "run_skill_snapshots"), ("ix_challenge_attachments_challenge_id", "challenge_attachments")):
        op.drop_index(index, table_name=table)
    for table in ("challenge_messages", "challenge_conversation_skills", "challenge_conversations", "run_skill_snapshots", "challenge_skill_bindings", "model_skill_bindings"):
        op.drop_table(table)
    with op.batch_alter_table("challenges") as batch:
        batch.drop_column("metadata_json")
        batch.drop_column("primary_attachment_id")
        batch.drop_column("challenge_type")
        batch.alter_column("target_url", existing_type=sa.String(2048), nullable=False)
    op.drop_table("challenge_attachments")
    op.drop_table("skills")
