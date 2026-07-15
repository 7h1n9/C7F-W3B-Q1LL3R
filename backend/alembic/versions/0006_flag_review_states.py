"""Add manual flag review states.

Revision ID: 0006_flag_review_states
Revises: 0005_solver_methodology_flow
Create Date: 2026-07-12
"""

import sqlalchemy as sa

from alembic import op

revision = "0006_flag_review_states"
down_revision = "0005_solver_methodology_flow"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "flag_candidates",
        sa.Column(
            "review_state",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'OPEN'"),
        ),
    )
    op.execute(
        sa.text(
            "UPDATE flag_candidates SET review_state = CASE WHEN verified = 1 THEN 'VALID' ELSE 'OPEN' END"
        )
    )


def downgrade() -> None:
    op.drop_column("flag_candidates", "review_state")
