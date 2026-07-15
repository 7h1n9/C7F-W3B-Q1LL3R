"""Add quarantined learned Skill candidate tables."""

import sqlalchemy as sa

from alembic import op

revision = "0011_learned_skill_candidates"
down_revision = "0010_skill_neg_triggers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "learned_skill_candidates" not in tables:
        op.create_table(
            "learned_skill_candidates",
            sa.Column("id", sa.String(36), primary_key=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("name", sa.String(160), nullable=False, unique=True), sa.Column("display_name", sa.String(220), nullable=False), sa.Column("description", sa.Text(), nullable=False), sa.Column("status", sa.String(30), nullable=False), sa.Column("content_markdown", sa.Text(), nullable=False), sa.Column("sanitized_content", sa.Text(), nullable=False), sa.Column("source_run_id", sa.String(36), sa.ForeignKey("solve_runs.id"), nullable=False), sa.Column("source_artifact_ids", sa.JSON(), nullable=False), sa.Column("source_observation_ids", sa.JSON(), nullable=False), sa.Column("metadata_json", sa.JSON(), nullable=False), sa.Column("security_scan_json", sa.JSON(), nullable=False), sa.Column("generalization_score", sa.Integer(), nullable=False),
        )
    if "learned_skill_candidate_sources" not in tables:
        op.create_table("learned_skill_candidate_sources", sa.Column("id", sa.String(36), primary_key=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False), sa.Column("candidate_id", sa.String(36), sa.ForeignKey("learned_skill_candidates.id"), nullable=False), sa.Column("source_type", sa.String(30), nullable=False), sa.Column("source_id", sa.String(255), nullable=False), sa.Column("detail_json", sa.JSON(), nullable=False))
    if "learned_skill_reviews" not in tables:
        op.create_table("learned_skill_reviews", sa.Column("id", sa.String(36), primary_key=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False), sa.Column("candidate_id", sa.String(36), sa.ForeignKey("learned_skill_candidates.id"), nullable=False), sa.Column("decision", sa.String(20), nullable=False), sa.Column("reviewer", sa.String(120), nullable=False), sa.Column("review_json", sa.JSON(), nullable=False))
    if "learned_skill_validation_runs" not in tables:
        op.create_table("learned_skill_validation_runs", sa.Column("id", sa.String(36), primary_key=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False), sa.Column("candidate_id", sa.String(36), sa.ForeignKey("learned_skill_candidates.id"), nullable=False), sa.Column("run_id", sa.String(36), sa.ForeignKey("solve_runs.id"), nullable=False), sa.Column("status", sa.String(30), nullable=False), sa.Column("result_json", sa.JSON(), nullable=False))


def downgrade() -> None:
    for table in ("learned_skill_validation_runs", "learned_skill_reviews", "learned_skill_candidate_sources", "learned_skill_candidates"):
        op.drop_table(table)
