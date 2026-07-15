"""Add solver state skill recommendations.

Revision ID: 0007_run_skill_recommendations
Revises: 0006_flag_review_states
Create Date: 2026-07-12
"""

import sqlalchemy as sa

from alembic import op

revision = "0007_run_skill_recommendations"
down_revision = "0006_flag_review_states"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return column in {item["name"] for item in inspector.get_columns(table)}


def upgrade() -> None:
    with op.batch_alter_table("solver_states") as batch:
        if not _has_column("solver_states", "skill_recommendations_json"):
            batch.add_column(
                sa.Column(
                    "skill_recommendations_json",
                    sa.JSON(),
                    nullable=True,
                )
            )
    if _has_column("solver_states", "skill_recommendations_json"):
        op.execute(
            sa.text(
                "UPDATE solver_states SET skill_recommendations_json = JSON_ARRAY() "
                "WHERE skill_recommendations_json IS NULL"
            )
        )
        with op.batch_alter_table("solver_states") as batch:
            batch.alter_column("skill_recommendations_json", existing_type=sa.JSON(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("solver_states") as batch:
        batch.drop_column("skill_recommendations_json")
